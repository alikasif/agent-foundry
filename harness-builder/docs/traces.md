# Traces

Every run produces a trace: a JSON file that records every LLM call, decision,
permission check, hook, tool call and result, context operation and budget change.
The trace is the single source of truth for what happened. It powers offline
replay (`harness replay`), comparison (`harness diff`) and inspection
(`harness trace show`).

- [Where traces go](#where-traces-go)
- [Schema v1](#schema-v1)
- [Volatile fields](#volatile-fields)
- [Redaction](#redaction)
- [Replay](#replay)
- [Diff](#diff)
- [CLI reference](#cli-reference)
- [Limits](#limits)

## Where traces go

`Harness.run` writes `<trace_dir>/<run_id>.json`. The default trace directory is
`.harness/traces`, relative to the working directory; override it with the
`HARNESS_TRACE_DIR` environment variable (`Settings.trace_dir`) or
`HarnessBuilder.trace_dir(path)`. `trace_dir(None)` keeps the trace in memory only.
The path is returned in `RunResult.trace_path`. `Harness.run_traced(task)` returns
`(RunResult, Trace)` and is what the CLI uses.

Run ids are 32 hex characters; they become file names, so ids must match
`[A-Za-z0-9_-]+`.

```python
from pathlib import Path

from harness_builder.trace.recorder import load_trace

trace = load_trace(Path(".harness/traces/<run_id>.json"))
print(trace.stop.reason, trace.usage)
```

## Schema v1

A `Trace` (`harness_builder.trace.models.Trace`, `schema_version: 1`) has these
fields.

| Field | Meaning |
|---|---|
| `schema_version` | Always `1`. |
| `run_id`, `parent_run_id`, `depth` | Identity; `depth` is 0 for the top-level run. |
| `created_at` | UTC timestamp. |
| `harness_name`, `strategy`, `model`, `task` | What ran. |
| `config_hash` | sha256 of the resolved harness config (see [extending.md](extending.md#what-goes-into-the-config-hash)). |
| `spec_snapshot` | The resolved `HarnessSpec` as a dict, when the harness was built from a spec (top-level runs only); `null` for code-built harnesses. Prompt files are inlined. |
| `steps` | List of `{index, events}`. One step is one LLM call plus its tool calls. |
| `usage` | Totals: `input_tokens`, `output_tokens`, `cost_usd` (None if any cost is unknown), `llm_calls`. Includes embedded sub-agent runs. |
| `stop` | `{reason, detail, step_index}`; reason is `done`, `budget`, `veto` or `error`. |
| `env` | `python`, `os`, and the versions of `harness_builder`, `adk`, `litellm` when installed. |

Each event has a `kind` discriminator:

| `kind` | Fields |
|---|---|
| `llm_call` | `purpose` (`step`, `summarize`, `promotion`), `request_fingerprint`, `request` (`LLMRequest`), `response` (`LLMResponse`), `usage`, `cost_usd`, `latency_ms` |
| `decision` | `calls`, `final`, `parse_error`: what the strategy parsed from the response |
| `permission` | `call_id`, `decision` (`allow`, `deny`, `ask`), `rule_id`, `approver_result` |
| `hook` | `event`, `hook` (name), `outcome`, `error` |
| `tool_call` | `call_id`, `name`, `args` (validated), `validation` (`ok`, `error`) |
| `tool_result` | `call_id`, `observation`, `duration_ms`, `nondeterministic` |
| `context` | `policy`, `tokens_before`, `tokens_after`, `refs_created` |
| `budget` | `snapshot`: steps, usage and limits after a charge |
| `subagent` | `call_id`, `child`: the full nested `Trace` of the child run |

Typical event order inside a step: `llm_call`, `budget`, `decision`, then per call
`tool_call`, `permission`, `hook` events of `pre_tool` hooks, `tool_result`, then
`hook` events of `post_tool` hooks. A `context` event
comes first when a context policy is set. A call that fails validation or is
denied still gets a `tool_result` event with the error; it has no `duration_ms`
(only calls the loop actually invoked do).

`request_fingerprint` is the sha256 of the canonical JSON of the **redacted**
request, volatile fields excluded. Replay uses it to check that the replayed run
asks the model exactly what the recording asked.

Inspect a trace with `harness trace show <file>`, or load it:

```python
from harness_builder.trace.models import LlmCallEvent

for step in trace.steps:
    for event in step.events:
        if isinstance(event, LlmCallEvent):
            print(step.index, event.purpose, event.usage.input_tokens)
```

## Volatile fields

These fields change between otherwise identical runs and are ignored by replay,
diff and equality checks (`trace.models.stable_dump`, `canonical_json`):

`Trace.run_id`, `Trace.parent_run_id`, `Trace.created_at`, `Trace.env`,
`LlmCallEvent.latency_ms`, `ToolResultEvent.duration_ms`, and the volatile fields
of nested models such as `ExecResult.duration_s`.

`spec_snapshot` is **not** volatile. A spec-built trace and a code-built trace of
the same harness differ in it (and in nothing else that matters), so strip it
before comparing the two.

## Redaction

Secrets are redacted when events are recorded, so a trace file never contains
them (`trace.redact.Redactor`).

- The values of every environment variable whose name contains `KEY`, `TOKEN`,
  `SECRET` or `PASSWORD` (case-insensitive) are replaced by `[REDACTED:<NAME>]`.
  Secret-named variables of the `./.env` file and `OPENROUTER_API_KEY` are covered
  too, even though `.env` values are never copied into `os.environ`.
- Values shorter than 6 characters are not redacted, so `X_KEY=1` does not mask
  every `1`. The name patterns and the length are **ASSUMED** (OQ-16).
- Redaction applies to every string in the event, including dict keys and nested
  sub-agent traces. Schema constants such as `kind` or `role` are never rewritten.
  The spec snapshot is redacted too, so a secret written into the spec itself
  does not survive a rebuild from the snapshot.
- `by_reference` files under `refs/` are written through the same redactor.
- Only secrets the process knows are redacted. A secret that is not in the
  environment, `.env` or settings (for example one typed by a user into the task)
  is stored as is. Treat trace files as sensitive anyway.
- The answer printed by `harness run` on stdout is not redacted; it is your result.

## Replay

Replay re-runs `trace.task` through a harness whose LLM, tool runner and approver
answer from the recording, so it is offline, free and needs no API key and no
Docker. It checks that the current code still behaves the way the recorded run
did, which is how you catch regressions in a strategy, a hook, a permission rule
or a context policy.

- `ReplayLLMClient` returns the recorded responses in order and compares each
  request fingerprint with the recorded one.
- `ReplayToolRunner` returns the recorded raw observation of each call, matched by
  `(step index, call_id)`, after checking the tool name and arguments. Tools are
  never executed.
- `ReplayApprover` answers `ask` decisions with the recorded answers.
- `post_tool` hooks, `pre_tool` hooks, permissions, validation, the context policy
  and the budget run for real.

The first difference is reported as a `ReplayDivergence` with a step index and a
kind:

| Kind | Meaning |
|---|---|
| `request` | The replayed LLM request differs from the recorded one (fingerprint mismatch), the recording ran out, or an expected call never happened. |
| `decision` | The strategy parsed the (same) response differently. |
| `tool_call` | A call, its arguments, or an approval does not match the recording. |
| `stop` | The run ended differently (reason, detail or step). |

### From the CLI

```text
uv run harness replay .harness/traces/<run_id>.json
Replay matched (1 steps).
```

`harness replay` rebuilds the harness from `trace.spec_snapshot`, so it only works
for traces of spec-built harnesses. For a code-built trace it exits with code 2 and
`trace was not built from a spec; replay needs spec_snapshot`.

### From Python (any harness)

Build the same harness as the recording, but feed it the session's components.

```python
import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool
from harness_builder.trace.replay import ReplaySession, replay


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


def make_builder() -> HarnessBuilder:
    return (
        HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini"))
        .tools(add)
        .permissions([Rule("add", "allow")])
        .budget(max_steps=5)
    )


def factory(session: ReplaySession):
    return (
        make_builder()
        .llm(session.llm, retry_delay_s=0)
        .tool_runner(session.tool_runner)
        .approver(session.approver)
        .trace_dir(None)
        .build()
    )


# `trace` is a recorded Trace, for example from load_trace(...)
report = anyio.run(replay, trace, factory)
print(report.matched, report.divergence)
```

`report.result` is the replayed `RunResult` and `report.trace` the replayed trace.
Rules for the factory: same strategy, prompts, tools, rules, hooks, budget and
context policy as the recording, and every LLM call (including a `summarize`
policy's) must go through `session.llm`.

Replay compares redacted forms. The replaying process redacts with its own
environment, so a secret that appeared in a request and whose value changed since the
recording shows up as a `request` divergence.

## Diff

`harness diff A B` compares two traces, typically two runs of the same task with
different strategies, prompts or models. Steps are aligned by index. For each step
it compares, in this order, `tool_names`, `tool_args`, `final` and `response`
(sha256 of the step LLM response text). The first mismatch is the first
divergence; `missing_step` is reported when one trace has a step the other lacks.
Call ids and volatile fields never take part.

In a `response` divergence, `a` and `b` are lists of sha256 hashes of the response
texts, not the texts themselves; open the traces (`harness trace show`) to read
them. For `tool_names`, `tool_args` and `final` the values are shown as is.

Below the divergence it prints deltas (`b - a`) for steps, input tokens, output
tokens and cost, a per-tool call count, and the stop reasons.

```text
uv run harness diff a.json b.json
uv run harness diff a.json b.json --json
```

The JSON form has `first_divergence` (`step_index`, `field`, `a`, `b`, or null),
`steps`, `input_tokens`, `output_tokens`, `cost_usd` (each `{a, b, delta}`),
`tool_usage` and `stop_reason`. In Python:

```python
from harness_builder.trace.diff import diff_traces

result = diff_traces(trace_a, trace_b)
print(result.identical, result.first_divergence)
```

Tool counts cover the calls the run attempted at its own level; sub-agent child
traces are not counted.

## CLI reference

```text
harness run TARGET TASK [--strategy NAME] [--trace FILE]
harness replay TRACE_FILE [--json]
harness diff A B [--json]
harness trace show TRACE_FILE
harness doctor [--clean]
```

- `run` runs `TASK` with the harness spec at `TARGET` (a spec file or a directory
  containing `harness.yaml`, `harness.yml` or `harness.json`). The answer goes to
  stdout; the stop line (`stop: done at step 0`) and the trace path go to stderr.
  `--trace FILE` writes the trace to that file instead of the trace directory.
  `--strategy NAME` overrides the spec's strategy (and drops its strategy options
  if the name differs); the override is recorded in the snapshot so replay
  matches.
- `replay --json` prints `{"matched", "stop", ...}` plus `step_index`, `kind`,
  `expected` and `actual` on divergence.
- `doctor` checks the Docker CLI, daemon and sandbox image and exits 3 on failure;
  see [docker.md](docker.md#harness-doctor).
- `trace show` renders a header table (harness, run id, model, strategy, task,
  config hash, whether a spec snapshot exists, usage, stop) and a tree of steps
  and events, with sub-agent steps nested.

| Exit code | Meaning |
|---|---|
| 0 | Success (`run` stopped `done`; `replay` matched; `diff` and `trace show` finished). |
| 1 | `run` stopped for any reason other than `done`; `replay` diverged. |
| 2 | Usage or configuration error: invalid spec, unknown strategy, missing API key or model, unreadable or invalid trace file, a trace without a spec snapshot. |
| 3 | Docker unavailable (only for harnesses with executor-backed tools, and `harness doctor`; see [docker.md](docker.md)). |

`diff` always exits 0 when both files load, even when the traces differ. Use
`--json` and check `first_divergence` in scripts.

## Limits

- Child (sub-agent) traces are recorded and shown, but replay does not re-run
  them: `spawn_agent` is marked nondeterministic, so replay returns the recorded
  observation of the call.
- `harness replay` needs a spec snapshot; code-built harnesses replay through the
  Python API.
- Replay of a `run_code` or CodeAct run skips Docker preflight and serves the
  recorded results (`run_code` is nondeterministic), so it needs no Docker. This
  was checked offline with scripted fakes only; no trace from a real container run
  exists yet.
