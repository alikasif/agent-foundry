# Extending harness-builder

This guide shows how to add a tool, a strategy, a hook and a context policy.
Every snippet was run against the implemented API.

**M1 limit: extensions are code-only.** The spec (`harness.yaml`) can only name
built-in tools and hooks, and in M1 the built-in hook registry is empty and the
only built-in tool is `run_code` ([strategies.md](strategies.md#the-run_code-tool)). A custom tool, hook, context
policy or approver therefore has to be attached in Python, either on a fresh
`HarnessBuilder` or chained after `HarnessBuilder.from_spec(...)`. The `harness
run` command cannot load custom code yet (that is planned work), so use the Python
API for anything custom.

- [Writing a tool](#writing-a-tool)
- [Writing a hook](#writing-a-hook)
- [Writing a context policy](#writing-a-context-policy)
- [Writing a strategy](#writing-a-strategy)
- [What goes into the config hash](#what-goes-into-the-config-hash)

## Writing a tool

Decorate a typed function with `@tool` (`harness_builder.tools.registry.tool`,
also re-exported from `harness_builder.tools`).

```python
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import ToolResult
from harness_builder.tools.registry import tool


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


@tool(timeout_s=5.0)
def finish(answer: str, ctx: RunContextView) -> ToolResult:
    """Submit the final answer (run ``ctx.run_id`` is available here)."""
    return ToolResult(content="submitted", final=answer)
```

Rules, as implemented:

- The name defaults to the function name and the description to the docstring.
  Without either, `tool()` raises `ConfigError`.
- The args model is derived from the signature. Every parameter needs a type hint;
  `*args`, `**kwargs` and positional-only parameters raise `ConfigError`. Extra
  arguments from the model are rejected (`extra="forbid"`). You can pass your own
  pydantic model with `@tool(args_model=MyArgs)`.
- A parameter named `ctx` is not part of the args model; the registry passes the
  `RunContextView` (`run_id`, `depth`, `workdir`, `messages`, `ledger`,
  `recorder`).
- The body is called with one keyword argument per args-model field, as the
  validated values.
- Sync functions run in a worker thread; `async def` functions are awaited.
  `timeout_s` cancels the call and returns a `timeout` observation. A sync body
  that overruns its timeout keeps running in an abandoned thread, because Python
  threads cannot be cancelled.
- `risk` tags (`network`, `filesystem_write`, `code_exec`, `subprocess`) are
  metadata for permissions and preflight. `nondeterministic=True` tells replay to
  always use the recorded output.
- Return a `str`, a `dict`, a pydantic model, `None`, a `ToolResult` or an
  `Observation`. Anything else is JSON-encoded. A `ToolResult` with `final` set
  ends the run with that answer (calls after it in the same step are skipped).
- Exceptions become a `tool_exception` observation; bad arguments become a
  `validation` observation that lists the fields and what is required. The model
  sees both and can retry.

### Register it, and allow it

Registering is not enough: **unlisted tools are denied** by default
(ASSUMED, OQ-22). Add a rule for the tool.

```python
import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.core.types import ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


script = [
    LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 2, "b": 3})]),
    LLMResponse(text="2 + 3 = 5"),
]
harness = (
    HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini"))
    .system_prompt("You are a calculator.")
    .tools(add)
    .permissions([Rule("add", "allow")])
    .budget(max_steps=5)
    .llm(FakeLLMClient(script))
    .trace_dir(None)
    .build()
)
result = anyio.run(harness.run, "What is 2 + 3?")
print(result.answer, result.stop.reason)  # 2 + 3 = 5 done
```

`.visible_tools(names)` limits which registered tools are advertised and callable.
A tool name must be unique; `build()` raises `ConfigError` for duplicates, for
visible names that are not registered, and when the strategy's `required_tools`
are missing.

Any object with `name`, `description`, `args_model`, `timeout_s`,
`nondeterministic`, `risk` and a callable `fn` also satisfies the registry
(`ToolDefinition` plus `fn`); `deref` and `spawn_agent` are built that way.

## Writing a hook

Three events, each a plain function (sync or `async`). `ctx` is the
`RunContextView`.

| Event | Signature | Return |
|---|---|---|
| `pre_tool` | `(call: ToolCall, ctx) -> HookOutcome \| None` | `allow`, `modify` (with `args`), `block` or `stop` |
| `post_tool` | `(call: ToolCall, obs: Observation, ctx) -> Observation \| None` | the (possibly rewritten) observation |
| `on_stop` | `(result: RunResult, ctx) -> HookOutcome \| None` | `allow` or `stop` (veto) |

`HookOutcome(action="allow", args=None, reason=None)` with actions `allow`,
`modify`, `block`, `stop`.

```python
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import HookOutcome, Observation, RunResult, ToolCall


def no_big_numbers(call: ToolCall, ctx: RunContextView) -> HookOutcome:
    """Refuse additions above 1000; the model sees a hook_blocked error."""
    if call.name == "add" and any(abs(v) > 1000 for v in call.args.values()):
        return HookOutcome(action="block", reason="numbers above 1000 are refused")
    return HookOutcome()


def mask_fives(call: ToolCall, obs: Observation, ctx: RunContextView) -> Observation:
    """Rewrite a string result before the model sees it."""
    if isinstance(obs.content, str):
        return obs.model_copy(update={"content": obs.content.replace("5", "#")})
    return obs


def require_five(result: RunResult, ctx: RunContextView) -> HookOutcome:
    """Veto answers that do not mention 5; the model is asked to continue."""
    if result.answer and "5" in result.answer:
        return HookOutcome()
    return HookOutcome(action="stop", reason="answer must mention 5")
```

Attach them with `HarnessBuilder.hook(event, fn, priority=0, name=None)`:

```python
from harness_builder import HarnessBuilder, Settings

builder = (
    HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini"))
    .hook("pre_tool", no_big_numbers, priority=10)
    .hook("post_tool", mask_fives)
    .hook("on_stop", require_five)
)
```

Semantics, as implemented in `HookManager`:

- Hooks run in ascending `priority`, then registration order. The default name is
  `module.qualname`, which is what appears in the trace and in the config hash.
- `None` is accepted and means allow (`pre_tool`, `on_stop`) or unchanged
  (`post_tool`).
- `pre_tool`: `modify` replaces the args for later hooks. The first `block` or
  `stop` short-circuits. `block` becomes a `hook_blocked` error observation;
  `stop` ends the whole run with reason `veto`. **A hook that raises, returns the
  wrong type, or returns `modify` without `args` fails closed as `block`.**
- Modified args are validated and permission-checked again before the call runs.
- `post_tool` and `on_stop`: a hook that raises or returns the wrong type is
  ignored and the error is recorded in the trace.
- `on_stop`: `stop` (or `block`) vetoes the proposed answer. The reason is sent to
  the model as a user message and the run continues. After 3 vetoes (ASSUMED) the
  run stops with reason `veto`.
- Every hook call is recorded as a `hook` trace event. The recorded tool result is
  the raw observation, before `post_tool` hooks; replay runs the hooks again.
- Hooks should be deterministic: replay re-executes them and compares results.

A spec cannot name hooks in M1 (`hooks:` entries need a registered builtin and
there are none yet).

## Writing a context policy

Implement `async apply(messages, ctx) -> list[Message]`. Return a new list; never
mutate `messages` (the canonical history). Keep the system messages and the task
message, and never start a kept tail with a `tool` message without the assistant
message that requested it, or providers will reject the history.

```python
from harness_builder.context.truncate import record_context
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import Message


class KeepLast:
    """Keep the leading system/task messages plus the last ``n`` messages."""

    name = "keep_last"

    def __init__(self, n: int = 4) -> None:
        self.n = n

    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]:
        head = messages[:2]
        kept = head + messages[2:][-self.n :]
        record_context(ctx, self.name, messages, kept, None)
        return kept
```

```python
from harness_builder import HarnessBuilder, Settings

builder = HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini")).context(
    KeepLast(4)
)
```

Notes:

- `record_context(ctx, policy_name, before, after, counter)` writes the `context`
  trace event (token counts before and after). Without it your policy still works
  but leaves no trace event.
- The task message is the first non-system message; `split_pinned(messages)` and
  `fit_tail(...)` in `harness_builder.context.truncate` are the helpers the
  built-in policies use.
- If your policy has a `counter` attribute set to `None`, the builder injects the
  harness token counter.
- A policy that calls an LLM must charge `ctx.ledger.record_llm(...)` and record an
  `llm_call` event itself, as `SummarizePolicy` does, and in a replayable harness
  it must use the injected `LLMClient` so that replay can answer the call.
- Built-in policies are chosen by name: `.context("truncate", max_tokens=2000)`.
  See [layers.md](layers.md#contextpolicy) for the options.

## Writing a strategy

A strategy owns the model-facing protocol: how the prompt looks, how a response
is parsed into actions, and how observations are shown back. It must not call the
LLM. See the `Strategy` protocol in [layers.md](layers.md#strategy).

The example below defines a text-only grammar: a reply starting with `CALL` is a
tool call, anything else is the final answer.

```python
import json

from harness_builder.core.types import (
    ErrorObservation,
    Message,
    ModelConfig,
    StepDecision,
    ToolCall,
    ToolOutcome,
)
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.strategies.registry import register_strategy


class LineStrategy:
    """`CALL <tool> <json args>` calls a tool; any other text is the answer."""

    name = "lines"
    required_tools: frozenset[str] = frozenset()

    def __init__(self, prefix: str = "CALL") -> None:
        self.prefix = prefix

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        listing = "\n".join(f"- {t.name}: {t.description}" for t in tools)
        rule = f"Reply `{self.prefix} <tool> <json args>` or plain text."
        return f"{base}\n{rule}\n{listing}"

    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest:
        split = 0
        while split < len(messages) and messages[split].role == "system":
            split += 1
        system = "\n\n".join(m.content for m in messages[:split]) or None
        return LLMRequest(model=model, system=system, messages=messages[split:])

    def parse(self, response: LLMResponse) -> StepDecision:
        text = response.text.strip()
        assistant = Message(role="assistant", content=text)
        if not text:
            error = ErrorObservation(type="format", message="empty reply")
            return StepDecision(assistant=assistant, parse_error=error)
        if not text.startswith(f"{self.prefix} "):
            return StepDecision(assistant=assistant, final=text)
        _, name, raw = text.split(" ", 2)
        call = ToolCall(id="line_call", name=name, args=json.loads(raw))
        return StepDecision(assistant=assistant, calls=[call])

    def render_observations(self, results: list[ToolOutcome]) -> list[Message]:
        return [
            Message(role="user", content=f"RESULT {o.observation.content}")
            for o in results
        ]


register_strategy("lines", LineStrategy)
```

Use it by name, with options:

```python
from harness_builder import HarnessBuilder, Settings

builder = HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini")).strategy(
    "lines", prefix="CALL"
)
```

or in a spec: `strategy: {name: lines, options: {prefix: CALL}}`, provided the
strategy was registered before the spec is loaded (`SpecError` for an unknown
name). You can also pass an instance: `.strategy(LineStrategy())`.

Contract details:

- `parse` returns a `StepDecision`: `calls` (tool calls), `final` (answer, no
  calls), or `parse_error` (an `ErrorObservation` the loop sends back to the
  model as a user message). The `assistant` message is appended to the history in
  every case.
- Give each call in one response a unique `id`; `render_observations` receives the
  matching `ToolOutcome` objects. Native-call strategies must answer every call id
  with a `tool` message. Text strategies may use `user` messages as above.
  Call ids may repeat across steps, so trace consumers key by step and id.
- `json.loads` above raises on malformed JSON, which the loop reports as an
  `error` stop. A production strategy should catch it and return a `format`
  `parse_error` instead.
- `required_tools` lists tool names that must be registered and visible;
  `build()` raises `ConfigError` otherwise.
- `register_strategy(name, factory)` is a no-op for the same factory and raises
  `ValueError` for a different one unless `replace=True`. A factory can also be a
  lazy `"package.module:attribute"` string, imported on first use. Options that
  the factory does not accept raise `ConfigError`.
- A plain conformance test for any strategy: scripted `FakeLLMClient`, one tool,
  one run, and `harness.run(...)` returns `done`. `tests/conformance/echo.py`
  (`EchoStrategy`) is a worked example of this contract.

## What goes into the config hash

`Trace.config_hash` is the sha256 of the resolved `HarnessConfig` (name, model,
strategy and options, system prompt, tool schemas, visible tools, context policy,
permissions, budget, hook names, sub-agent profiles). Two harnesses with equal
settings, built from code or from a spec, get the same hash.

- A tool contributes its schema (name, description, JSON schema), not its body.
  Changing only the function body does not change the hash.
- A hook contributes its name (`module.qualname` unless you pass `name=`).
- A context policy contributes its `name` and its public scalar and pydantic
  attributes (underscore-prefixed attributes are skipped).
- A strategy given by name contributes the name and the options you passed. A
  strategy given as an **instance** contributes only its `name`, not its options.
- Tool order matters: schemas are hashed in registration order. Tools named in a
  spec are registered before tools you chain afterwards.
- The LLM client, approver, tool runner and trace directory are runtime only and
  not hashed.
