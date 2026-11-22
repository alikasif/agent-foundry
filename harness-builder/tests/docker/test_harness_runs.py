"""Real-Docker runs through the harness: ``run_code`` + ``FakeLLMClient`` (U1).

FR-040's "no leak" holds at harness-run level: files in ``/work`` persist
between blocks of one run but never reach the next run, and Python state never
survives a block. The API key in ``Settings`` never reaches the container.
"""

import json
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.preflight import CONTAINER_LABEL
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.trace.models import ToolResultEvent, Trace

from .support import container_ids  # pyrefly: ignore[missing-import]

pytestmark = [pytest.mark.docker, pytest.mark.timeout(900)]

API_KEY = "sk-or-v1-t032-harness-secret-4b1d"


def code_call(code: str, i: int = 0) -> LLMResponse:
    """A ReAct response calling ``run_code`` with Python ``code``."""
    call = ToolCall(
        id=f"c{i}", name="run_code", args={"language": "python", "code": code}
    )
    return LLMResponse(text="", tool_calls=[call])


def harness(executor: DockerExecutor, *script: LLMResponse) -> Harness:
    """A ReAct harness with ``run_code`` allowed on the real ``executor``."""
    settings = Settings.model_validate(
        {"model": "fake/model", "openrouter_api_key": API_KEY}
    )
    return (
        HarnessBuilder("t032", settings=settings)
        .llm(FakeLLMClient(list(script)), retry_delay_s=0)
        .tools(run_code(timeout_s=60))
        .executor(executor)
        .permissions(rules=[Rule("run_code", "allow")])
        .trace_dir(None)
        .build()
    )


def tool_contents(trace: Trace) -> list[Any]:
    """The ``content`` of every top-level tool result, in order."""
    return [
        e.observation.content
        for s in trace.steps
        for e in s.events
        if isinstance(e, ToolResultEvent)
    ]


@pytest.mark.anyio
async def test_files_persist_within_run_but_python_state_does_not(
    executor: DockerExecutor,
) -> None:
    probe = (
        "import json, os\n"
        "print(json.dumps({'file': open('/work/note.txt').read(),"
        " 'global': 'counter' in globals()}))\n"
        "FINAL('checked')\n"
    )
    h = harness(
        executor,
        code_call("counter = 1\nopen('/work/note.txt', 'w').write('kept')\n", 0),
        code_call(probe, 1),
    )

    result, trace = await h.run_traced("t")

    assert (result.stop.reason, result.answer) == ("done", "checked")
    second = tool_contents(trace)[1]
    assert isinstance(second, dict)
    assert json.loads(second["stdout"].strip()) == {"file": "kept", "global": False}


@pytest.mark.anyio
async def test_no_file_or_state_leaks_across_two_harness_runs(
    executor: DockerExecutor,
) -> None:
    writer = harness(
        executor,
        code_call(
            "open('/work/leak.txt', 'w').write('secret-state')\n"
            "FINAL(open('/work/leak.txt').read())\n"
        ),
    )
    reader = harness(
        executor,
        code_call("import json, os\nFINAL(json.dumps(sorted(os.listdir('/work'))))\n"),
    )

    first, _ = await writer.run_traced("write")
    second, _ = await reader.run_traced("read")

    assert (first.stop.reason, first.answer) == ("done", "secret-state")
    assert second.stop.reason == "done"
    assert second.answer is not None
    assert "leak.txt" not in json.loads(second.answer)


@pytest.mark.anyio
async def test_api_key_never_reaches_container_through_harness(
    executor: DockerExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    h = harness(
        executor,
        code_call(
            "import json, os\n"
            "env = dict(os.environ)\n"
            "FINAL(json.dumps({'has_key': 'OPENROUTER_API_KEY' in env,"
            " 'leak': any('sk-or-v1' in v for v in env.values())}))\n"
        ),
    )

    result, trace = await h.run_traced("t")

    assert result.answer is not None
    assert json.loads(result.answer) == {"has_key": False, "leak": False}
    assert API_KEY not in trace.model_dump_json()


@pytest.mark.anyio
async def test_harness_runs_leave_no_containers(executor: DockerExecutor) -> None:
    before = container_ids(CONTAINER_LABEL)
    h = harness(executor, code_call("print(1)", 0), code_call("FINAL('ok')", 1))

    result, trace = await h.run_traced("t")

    assert result.answer == "ok"
    assert container_ids(f"hb.run={trace.run_id}") == set()
    assert container_ids(CONTAINER_LABEL) - before == set()
