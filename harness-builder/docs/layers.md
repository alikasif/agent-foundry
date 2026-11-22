# Layer reference

harness-builder is a stack of small layers. Each layer is a `typing.Protocol` in
`harness_builder.core.protocols` with a default implementation and, where it
matters, a test double. `HarnessBuilder` (in `harness_builder.core.builder`) is the
only place that wires concrete classes together; the step loop (`StepLoop`) only
knows the protocols.

This page describes what the code does today (milestone M1). Items marked
**ASSUMED** are defaults chosen during design that the user has not confirmed
(see `shared/prd.md`). Treat them as changeable.

- [Public import surface](#public-import-surface)
- [The step loop](#the-step-loop)
- [Layer table](#layer-table)
- [Layer details](#layer-details)
- [Defaults at a glance](#defaults-at-a-glance)
- [Not in M1](#not-in-m1)

## Public import surface

`import harness_builder` is cheap: names are resolved lazily and ADK, litellm and
the sibling packages are not imported. These names are exported from the top
level: `HarnessBuilder`, `Harness`, `Settings`, the value types
(`Message`, `ToolCall`, `Observation`, `RunResult`, `StopInfo`, `Usage`,
`BudgetLimits`, `HookOutcome`, ...), `LLMRequest`, `LLMResponse`, `ToolSchema`,
`Trace`, `TraceStep` and the errors (`HarnessError`, `ConfigError`, `SpecError`,
`ReplayDivergence`, `DockerUnavailableError`).

Everything else is imported from its module, for example:

```python
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool
```

All value types (`core/types.py`, `llm/types.py`, `trace/models.py`) are frozen
pydantic models with `extra="forbid"`. Build a modified copy with
`model_copy(update=...)`.

## The step loop

One run is a sequence of steps. A step is one LLM call plus the sequential
execution of every tool call that response contained.

1. `Budget.check_before_step()` counts the step and may stop the run (`budget`).
2. The `ContextPolicy` (if any) turns the canonical history into a working copy.
3. The `Strategy` builds the `LLMRequest`; the loop calls the `LLMClient`,
   retrying transient errors (2 retries, backoff `0.5 * 2**attempt` seconds;
   both **ASSUMED**).
4. The ledger is charged and `llm_call` and `budget` events are traced.
5. The strategy parses the response into a `StepDecision`: tool calls, a final
   answer, or a `format` parse error (reported back to the model).
6. Each call runs through the same pipeline:
   **validate -> permission -> `pre_tool` hooks -> invoke -> `post_tool` hooks**.
7. The strategy renders the observations back into messages.
8. A final answer runs the `on_stop` hooks, which may veto it (up to 3 times,
   **ASSUMED**, then the run stops with `veto`).

Stop reasons (`StopInfo.reason`): `done`, `budget`, `veto`, `error`.

Failures of tools, permissions and hooks never raise out of the loop. They become
an `Observation(ok=False, error=ErrorObservation(...))` that is shown to the
model. Error types: `validation`, `unknown_tool`, `tool_exception`,
`permission_denied`, `timeout`, `hook_blocked`, `format`, `depth_exceeded`,
`executor_error`. Only an LLM failure after retries or an internal error stops
the run with `error`.

When a single response contains several calls, they run in order. After a call
that sets a final answer (`Observation.final`), the remaining calls are not
executed and get a "not executed" error observation.

## Layer table

| Layer (protocol) | Default implementation | Test or replay double |
|---|---|---|
| `LLMClient` | `llm.adk_litellm.AdkLiteLlmClient` (ADK `LiteLlm` over OpenRouter) | `llm.fake.FakeLLMClient`, `trace.replay.ReplayLLMClient` |
| `Strategy` | `strategies.react.ReActStrategy`, looked up through `strategies.registry` | `EchoStrategy` (tests only, `tests/conformance/echo.py`) |
| `Budget` | `budget.ledger.BudgetLedger` | none needed |
| `TraceSink` | `trace.recorder.TraceRecorder` | none needed |
| `ToolRegistry` | `tools.registry.ToolRegistry` | none needed |
| `ToolRunner` | the registry's own `invoke`; `tools.builtin.spawn.SubagentRunner` when sub-agents are on | `trace.replay.ReplayToolRunner` |
| `ContextPolicy` | none (the full history is sent); built in: `TruncatePolicy`, `SummarizePolicy`, `ByReferencePolicy` | any object with `apply` |
| `PermissionPolicy` | `permissions.policy.RulePermissionPolicy` (first match, default deny) | `NarrowedPolicy` (sub-agents) |
| `Approver` | `CliApprover` on a TTY, else `AutoDenyApprover` (`default_approver()`) | `StaticApprover`, `trace.replay.ReplayApprover` |
| `HookManager` | `hooks.manager.HookManager` | none needed |
| `TokenCounter` | `llm.tokens.HeuristicCounter` (chars / 4) | `LiteLlmCounter` is the real tokenizer |
| `Executor` | `executor.docker.DockerExecutor` ([docker.md](docker.md), [isolation.md](isolation.md)) | `executor.fake.FakeExecutor` |
| `Retriever` | `skills.retriever.LexicalRetriever` (BM25F; [skills.md](skills.md)) | any `Retriever` implementation |

## Layer details

### LLMClient

```python
class LLMClient(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse: ...
```

`LLMRequest` carries `model: ModelConfig`, `system`, `messages`, `tools`
(`ToolSchema` list) and `purpose` (`"step"`, `"summarize"` or `"promotion"`).
`LLMResponse` carries `text`, `tool_calls`, `usage`, `finish_reason`, `model`.
The call is non-streaming.

`AdkLiteLlmClient(settings)` calls OpenRouter through ADK's `LiteLlm`. It raises
`ConfigError` at construction when `OPENROUTER_API_KEY` or `HARNESS_MODEL` is
missing. ADK's `Runner` is deliberately not used, so permissions, hooks, budget
and the trace stay under the loop's control. Provider exceptions propagate to the
loop, which retries the transient ones.

`FakeLLMClient(script)` returns scripted responses in order (or calls a function
`LLMRequest -> LLMResponse`) and records every request in `.requests`. A response
with default (empty) `usage` gets deterministic token counts from
`HeuristicCounter`, so budgets work offline. Calling it more often than scripted
raises `FakeScriptExhaustedError`.

### Strategy

```python
class Strategy(Protocol):
    name: str
    required_tools: frozenset[str]

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str: ...
    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest: ...
    def parse(self, response: LLMResponse) -> StepDecision: ...
    def render_observations(self, results: list[ToolOutcome]) -> list[Message]: ...
```

A strategy translates between messages and actions and never calls the LLM.
`ReActStrategy` (`name = "react"`) uses native function calling: tool schemas
travel as declarations, a response with tool calls becomes `calls`, call-free text
becomes the final answer, and an empty response or a nameless call is a `format`
parse error that the loop feeds back. It requires no tools.

`strategies.registry` maps names to factories: `register_strategy(name, factory,
replace=False)`, `get_strategy(name, options)` and `strategy_names()`. A factory
is a callable that takes the options as keyword arguments, or a lazy
`"package.module:attribute"` string. `"codeact"` is registered lazily (see [strategies.md](strategies.md)). See [extending.md](extending.md#writing-a-strategy).

### Budget

`BudgetLedger` enforces `BudgetLimits(max_steps=10, max_input_tokens=None,
max_output_tokens=None, max_cost_usd=None)`. The `max_steps` default is
**ASSUMED**; `None` means unlimited.

- `check_before_step()` counts the step and stops once the count exceeds
  `max_steps`, so exactly `max_steps` steps run.
- `record_llm(usage, model)` charges the ledger and every ancestor and stops when
  a token or cost total reaches its limit (`>=`).
- Cost per call is the model's price times the tokens; without a known price the
  provider-reported `usage.cost_usd` is used. If a cost cap is set and the cost is
  unknown, the run stops (fail closed). `HarnessBuilder.build()` already rejects a
  cost cap for a model with no known price.
- `child(fraction)` makes a sub-ledger holding that fraction of the remaining
  budget; its charges propagate to the parent. Sub-agents use this.

Prices come from `llm.pricing.PriceTable`: user overrides, then the package
`pricing.yaml`, then litellm's bundled cost map (read from disk, never fetched).
The prices in `pricing.yaml` are **ASSUMED** copies of list prices. Override with
`HarnessBuilder.pricing(PriceTable(overrides={...}).price)`.

### TraceSink

`TraceRecorder` collects events per step, redacts them as they are recorded, and
`finish(stop)` builds the `Trace`, writes `<trace_dir>/<run_id>.json` and returns
it. See [traces.md](traces.md).

### ToolRegistry and ToolRunner

`ToolRegistry` holds tools, advertises `ToolSchema` objects, validates arguments
and invokes tool bodies under their timeout. It never raises tool errors; they
become observations. The `@tool` decorator and the `Tool` model are described in
[extending.md](extending.md#writing-a-tool).

`ToolRunner.invoke(vcall, ctx)` is what the loop calls to execute a validated
call. The default is the registry itself. `SubagentRunner` routes `spawn_agent`
calls to the spawn tool and everything else to the registry; replay substitutes
`ReplayToolRunner`, which returns recorded observations and never runs a tool.

### ContextPolicy

```python
class ContextPolicy(Protocol):
    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]: ...
```

A policy returns a working copy for one step; the canonical history is never
edited. System messages and the task message are always kept. All built-in
policies are chosen with `HarnessBuilder.context(name_or_instance, **options)` and
trace a `context` event with `tokens_before` and `tokens_after`.

| Name | Options (defaults ASSUMED) | Behavior |
|---|---|---|
| `truncate` | `max_tokens` (required), `keep_last=12` | Keeps system, task and the longest recent tail that fits `max_tokens`. A tail never starts with a `tool` message. |
| `summarize` | `max_tokens` (required), `keep_last=6`, `model` (summarizer model, default the harness model) | Above `max_tokens`, folds older turns into a running summary with one extra LLM call (`purpose="summarize"`), charged to the budget and traced. |
| `by_reference` | `max_tokens` (required), `keep_last=12`, `ref_threshold_chars=4000`, `preview_chars=500`, `run_dir` | Replaces a tool message longer than the threshold with a `ref://<id>` stub plus preview. The `deref` tool reads slices back (max 16000 characters per call, **ASSUMED**). |

`by_reference` stores content under the first 16 hex characters of its sha256, in
memory and, when `run_dir` is set, in `<run_dir>/refs/<id>.txt` (redacted).
`build()` registers `deref` automatically and prepends an allow rule for it.

The token counts used for these limits come from the harness `TokenCounter`
(`HarnessBuilder.token_counter(...)`, default `HeuristicCounter`: `ceil(chars / 4)`).

### PermissionPolicy and Approver

```python
Rule("add", "allow")  # tool glob -> decision
Rule("write_*", "ask", args={"path": "/tmp/*"})  # every listed arg must match
```

`RulePermissionPolicy(rules, default="deny")` returns the decision of the first
matching rule, otherwise the default. **Unlisted tools are denied by default
(ASSUMED, OQ-22)**, so every tool needs a rule, or `default="allow"`/`"ask"` must
be set explicitly: `HarnessBuilder.permissions(rules, default=...)`.

Globs use `fnmatch.fnmatchcase`. `*` also matches `/` and `..`, so an argument
glob is not a path guard. Non-string argument values are matched as their
`json.dumps(..., sort_keys=True)` text. Rules see the validated, normalized
arguments.

A decision of `ask` goes to the `Approver`. `CliApprover` prompts on the terminal
(default no; EOF or Ctrl-C is no), `AutoDenyApprover` denies everything and is the
default when stdin is not a TTY, and `StaticApprover(answer)` is a fixed answer
that records requests (for tests). Every approver fails closed.

Rules are automatically added in front for `deref` (when `by_reference` is used)
and `spawn_agent` (when `.subagents()` is used), unless a rule already names
`spawn_agent` exactly.

### HookManager

Hooks run in ascending priority, then registration order. Events: `pre_tool`,
`post_tool`, `on_stop`. See [extending.md](extending.md#writing-a-hook) for
signatures and semantics.

### Sub-agents

`HarnessBuilder.subagents(max_depth=2, profiles=None)` registers the `spawn_agent`
tool. Each `ChildProfile` may override model, strategy, system prompt and a
subset of the parent's tools; `permissions` rules can only narrow the parent's
policy (`deny > ask > allow`). A child gets `budget_fraction` (default 0.5,
**ASSUMED**) of the parent's remaining budget, runs sequentially, and its full
trace is embedded in the parent trace as a `subagent` event. `max_depth` defaults
to 2 (**ASSUMED**, OQ-24). Child harnesses are built eagerly, so a bad profile
fails in `build()`.

### TokenCounter

`HeuristicCounter` is deterministic and dependency free. `LiteLlmCounter(model)`
uses litellm's tokenizer and imports litellm on first use.

## Defaults at a glance

All numbers below are design placeholders (**ASSUMED**) unless stated otherwise.

| Setting | Default |
|---|---|
| Strategy | `react` |
| `max_steps` | 10 |
| Token and cost limits | unlimited |
| Permission default | deny |
| `truncate` / `by_reference` `keep_last` | 12 |
| `summarize` `keep_last` | 6 |
| `ref_threshold_chars` / `preview_chars` | 4000 / 500 |
| LLM retries on transient errors | 2 |
| `on_stop` veto cap | 3 |
| Sub-agent `max_depth` / `budget_fraction` | 2 / 0.5 |
| Trace directory | `.harness/traces` (relative to the working directory) |
| Secret minimum length for redaction | 6 characters |

## Not in M1

- No live OpenRouter run has been made yet; everything here was exercised with
  `FakeLLMClient` and replay. `AdkLiteLlmClient` is covered by unit tests with a
  stubbed ADK, not by a real call.
- The Docker executor, `run_code`, the CodeAct strategy and `harness doctor` are
  M2 features, documented in [docker.md](docker.md), [isolation.md](isolation.md)
  and [strategies.md](strategies.md). Docker has not been exercised against a
  real daemon yet.
- The skills layer (`Retriever`, skill store, promotion) is implemented since M4
  and documented in [skills.md](skills.md); it has not run against real Docker.
