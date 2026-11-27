"""One chat: the agent, its history, and the sandbox that lives as long as it does."""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from harness.agent.build import build_agent
from harness.prompts.load import load_system_prompt, load_tool_descriptions
from harness.sandbox.channel import finish
from harness.sandbox.container import (
    Container,
    ContainerSpec,
    SandboxError,
    ensure_image,
)
from harness.sandbox.interpreter import Interpreter
from harness.sandbox.shell import run_bash
from harness.tools.plan.tool import PlanState
from harness.tools.registry import build_tools
from harness.workspace.changes import find_changes
from harness.workspace.snapshot import SessionCopy, create_copy, remove_copy

_APP = _USER = "sih"
_CANCEL_LIMIT_S = 30
_CLOSE_LIMIT_S = 10
# -f: a tracked file that .gitignore names was copied too, and belongs in it.
_BASELINE = (
    "git init -q && git add -A -f && "
    "git -c user.name=sih -c user.email=sih@localhost "
    "commit -q --allow-empty -m baseline"
)


@dataclass(frozen=True)
class Step:
    """One thing that happened in a turn.

    ``kind`` is "text", "tool_call", "tool_result" or "error". ``name`` is the
    tool's, if any. ``data`` is ``{"text"}``, the arguments, the result or
    ``{"message"}``.
    """

    kind: str
    name: str
    data: dict


def _failed(message: str) -> Step:
    return Step("error", "", {"message": message})


def _steps(event: Event) -> Iterator[Step]:
    if event.partial:
        return
    if event.error_code or event.error_message:
        yield _failed(f"{event.error_code}: {event.error_message}")
    for part in (event.content.parts if event.content else None) or []:
        if part.thought:
            continue
        if call := part.function_call:
            yield Step("tool_call", call.name or "", dict(call.args or {}))
        elif answer := part.function_response:
            yield Step("tool_result", answer.name or "", dict(answer.response or {}))
        elif part.text:
            yield Step("text", "", {"text": part.text})


class ChatSession:
    """A conversation with the agent. One turn at a time.

    With a ``container``, it is frozen between turns, so that nothing left running
    there can change the copy while the user reads a diff or applies it.
    """

    def __init__(
        self,
        agent: LlmAgent,
        plan_state: PlanState,
        *,
        max_llm_calls: int = 30,
        container: Container | None = None,
        copy: SessionCopy | None = None,
    ) -> None:
        self._runner = Runner(
            app_name=_APP,
            agent=agent,
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )
        self._session_id = uuid.uuid4().hex
        self._plan_state = plan_state
        self._run_config = RunConfig(max_llm_calls=max_llm_calls)
        self._container = container
        self._copy = copy
        self._turn: asyncio.Task | None = None

    @property
    def copy(self) -> SessionCopy:
        """What the agent works on."""
        if self._copy is None:
            raise ValueError("this session was made without a copy")
        return self._copy

    async def send(self, message: str) -> AsyncGenerator[Step]:
        """Run one turn. A failure is an ``error`` step, and the chat goes on.

        Read it to its end, or close it (``contextlib.aclosing``): that is what
        ends the turn. Raises ``SandboxError`` if a cancelled or closed turn
        could not freeze the sandbox.
        """
        self._plan_state.reset()
        self._turn = asyncio.current_task()
        reported = None
        try:
            try:
                try:
                    await self._unpause()
                    async with aclosing(self._events(message)) as events:
                        async for event in events:
                            for step in _steps(event):
                                if step.kind == "error":
                                    reported = step
                                yield step
                except Exception as error:  # noqa: BLE001 - the chat must go on
                    failed = _failed(f"{type(error).__name__}: {error}")
                    if failed != reported:  # ADK may have sent it as an event too
                        yield failed
            except BaseException:  # cancelled or closed
                await finish(self._pause())
                raise
            try:
                await finish(self._pause())
            except SandboxError as error:
                yield _failed(f"The sandbox was not paused: {error}")
        finally:
            self._turn = None

    async def close(self) -> None:
        """Cancel the running turn, if another task runs one, and close ADK."""
        turn = self._turn
        try:
            if turn and turn is not asyncio.current_task() and not turn.done():
                turn.cancel()
                done, _ = await asyncio.wait([turn], timeout=_CANCEL_LIMIT_S)
                if not done:
                    raise TimeoutError("the running turn did not stop")
        finally:
            await self._runner.close()

    def _events(self, message: str):
        content = types.Content(role="user", parts=[types.Part(text=message)])
        return self._runner.run_async(
            user_id=_USER,
            session_id=self._session_id,
            new_message=content,
            run_config=self._run_config,
        )

    async def _pause(self) -> None:
        if self._container is not None:
            await self._container.pause()

    async def _unpause(self) -> None:
        if self._container is not None:
            # To its end: a client left behind could resume after the next pause.
            await finish(self._container.unpause())


async def _commit_baseline(container: Container) -> None:
    """A repository for the agent, made in the container: never on the host."""
    result = await run_bash(container, _BASELINE)
    if result.exit_code != 0:
        reason = result.output.strip() or result.error
        raise SandboxError(f"the baseline commit failed: {reason}")


async def _release(
    container: Container, interpreter: Interpreter, copy: SessionCopy
) -> None:
    """Stop what ran, and remove the copy if nothing in it is left to apply."""
    # Neither matters if it fails: removing the container ends everything in it.
    with suppress(Exception):
        await container.unpause()  # a frozen container takes no signals
    with suppress(Exception):
        # It waits for a running call, hence the limit.
        await asyncio.wait_for(interpreter.close(), _CLOSE_LIMIT_S)
    try:
        await container.stop()
    finally:
        # If the changes cannot be found, this raises and the copy stays.
        if not find_changes(copy):
            remove_copy(copy)


@asynccontextmanager
async def open_session(
    llm: BaseLlm, source: Path, *, network: bool, image_dir: Path, sessions_dir: Path
) -> AsyncGenerator[ChatSession]:
    """A chat about a copy of ``source``, in a sandbox of its own.

    May be entered and left from different tasks of one event loop. On leaving,
    the copy is removed unless it holds changes.
    """
    image = ensure_image(image_dir)
    copy = create_copy(source, sessions_dir)
    container = Container(ContainerSpec(image, copy.work, network=network))
    interpreter = Interpreter(container)
    chat = None
    # No task group or timeout may span the yield: it would belong to one task.
    try:
        await container.start()
        await _commit_baseline(container)
        await container.pause()
        plan_state = PlanState()
        descriptions = load_tool_descriptions()
        tools = build_tools(container, interpreter, plan_state, descriptions)
        agent = build_agent(llm, tools, load_system_prompt(), plan_state)
        chat = ChatSession(agent, plan_state, container=container, copy=copy)
        yield chat
    finally:
        try:
            if chat is not None:
                await chat.close()
        finally:
            await finish(_release(container, interpreter, copy))
