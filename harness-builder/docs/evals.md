# Evals

An **eval** runs cases against a harness and reports pass or fail, steps, tokens
and cost per case. It works on any harness: hand-written, built from a
[spec](spec.md) or generated ([generator.md](generator.md)). Validation stage C
uses the same runner.

There are two kinds of case and five kinds of check.

> **Not run live.** The examples were run offline, with a scripted fake model and
> `--trust-tools`. No live LLM run and no real-Docker run has been made, so the
> `py_assert` check and Docker-runtime tools were never run in a container; on a
> machine without Docker they report `incomplete`.

- [Case files](#case-files)
- [ToolCase](#toolcase)
- [TaskCase](#taskcase)
- [Checks](#checks)
- [Statuses](#statuses)
- [Running evals: harness eval](#running-evals-harness-eval)
- [The runner from Python](#the-runner-from-python)
- [Metrics](#metrics)
- [Known gaps](#known-gaps)

## Case files

A spec's `evals` key is either a file (`evals: {file: evals/cases.yaml}`) or an
inline list. A file is YAML or JSON, a list of cases. Each case carries an
explicit `kind`, each check an explicit `type`, and unknown keys are rejected
with a located error such as `cases.yaml: [0].input: Field required` (file) or
`evals[0].check.text: ...` (inline).

```yaml
- kind: tool
  id: three-words
  tool: count_words
  args: {text: "one two three"}
  check: {type: equals, value: 3}
- kind: tool
  id: empty
  tool: count_words
  args: {text: ""}
  check: {type: py_assert, code: "assert answer == 0"}
- kind: task
  id: ask
  input: How many words are in "a b c d"?
  check: {type: contains, text: "4"}
  runs: 2
```

## ToolCase

`kind: tool`. A deterministic case: call one tool once with fixed arguments and
check the result. No model is involved and it costs 0 tokens.

| Field | Type | Notes |
|---|---|---|
| `kind` | `tool` | |
| `id` | string | Case name in reports. |
| `tool` | string | Tool name: a module tool's function name or a built-in. |
| `args` | object | Default `{}`. |
| `check` | a [check](#checks) | |

How it runs: the case is a real one-call harness run driven by a private
scripted model, so the normal path applies: visibility, argument validation,
permissions, approver, hooks, executor binding, the workdir and a trace. The
check sees the tool's **raw** result (before `post_tool` hooks). Consequences:

- The tool must be **allowed by the harness's permission rules**. A denied call
  fails the case with `permission_denied: ...`.
- Bad arguments fail with `validation: ...`, an unknown tool with
  `unknown_tool: ...`. A tool result with `ok: false` always fails the case,
  whatever the check.
- A trace is written for every case, ToolCases included (`trace: <case>: <path>`
  on stderr in the CLI).

## TaskCase

`kind: task`. An agent case: run the harness on `input` and check the final
answer. It needs a model.

| Field | Type | Notes |
|---|---|---|
| `kind` | `task` | |
| `id` | string | |
| `input` | string | The task given to the harness. |
| `check` | a [check](#checks) | |
| `runs` | integer, at least 1 | Default 1. `harness eval --runs N` overrides it for every TaskCase. |

A run is checked only when it stopped with `done` and produced an answer. A
`budget`, `veto` or `error` stop fails the run with
`run stopped with <reason>: <detail>`. A case passes only when **every** run
passed. TaskCases run only with `--live`; otherwise they are `not_run`.

## Checks

| `type` | Fields | Passes when |
|---|---|---|
| `contains` | `text` | `text` occurs in the answer. A dict answer is compared as sorted JSON. |
| `regex` | `pattern` | `re.search(pattern, answer)` matches. |
| `equals` | `value` | The answer equals `value`. A dict is compared directly. A string is compared after `strip()`. For a non-string `value`, the answer is `json.loads`-ed first, because tools returning ints or lists are JSON-encoded (`add` returns `"3"`). |
| `py_assert` | `code`, `timeout_s` (default 30) | The code exits 0 in the sandbox with `answer` bound. A timeout or an OOM kill fails. Needs Docker. |
| `rubric` | `text`, `judge_model` | A judge model replies with `PASS` as the first word. Live only. |

Details worth knowing:

- Field names differ by check: `contains` uses `text`, `equals` uses `value`.
- `py_assert` runs your code after a prelude
  `import json, sys; answer = json.loads(sys.stdin.read())`. The answer is passed
  as JSON on stdin and is never spliced into the code. Without an executor or
  Docker the case is `incomplete`.
- `rubric` builds one judge request at temperature 0. Without a judge, or if the
  judge raises, the case is `incomplete`. Judge tokens are not counted in the
  case's tokens.

## Statuses

| Status | Meaning |
|---|---|
| `pass` | Every run passed. |
| `fail` | At least one run failed. |
| `not_run` | Skipped by design: a TaskCase or `rubric` check without `--live`. |
| `incomplete` | Could not be decided: Docker is unavailable (harness build or `py_assert` preflight), or a `py_assert` has no executor, or a `rubric` has no judge. Never counted as a pass. |

A case is `fail` if any run failed, else `incomplete` if any run was
incomplete, else `pass`. A report is `ok` only when nothing failed and nothing
was incomplete. A `ConfigError` while building the harness (for example an
untrusted `runtime: host` tool) is not a case status; it ends the command with
exit 2.

## Running evals: harness eval

```text
uv run harness eval TARGET [--runs N] [--live] [--trust-tools] [--allow-failed]
```

| Option | Meaning |
|---|---|
| `--runs N` | Runs per TaskCase, overriding each case's `runs`. At least 1. |
| `--live` | Also run TaskCases and Rubric checks with the real LLM. |
| `--trust-tools` | Allow `runtime: host` tools and module hooks (runs them on the host). `runtime: docker` tools still run in Docker. |
| `--allow-failed` | Evaluate a generated harness whose `.harness-meta.json` status is `fail`. |

Exit codes: 0 when every decided case passed, 1 when any case failed, 3 when a
case is `incomplete` and none failed, 2 for a config or usage error. A failure
outranks incomplete.

Output is a table on stdout, `trace:` lines on stderr and a totals line. A real
offline run (host-runtime tool, `--trust-tools`, `--live`, a scripted model):

```text
$ uv run harness eval word-counter --trust-tools --live --runs 2 --fake-llm scripted.json
                                 harness eval                                  
+------------------------------------------------------------------------+
| case        | kind | status | rate | steps | tokens | cost    | detail |
|-------------+------+--------+------+-------+--------+---------+--------|
| three-words | tool | pass   | 100% | 2     | 0      | $0.0000 | -      |
| empty       | tool | pass   | 100% | 2     | 0      | $0.0000 | -      |
| ask         | task | pass   | 100% | 1     | 140    | $0.0000 | -      |
+------------------------------------------------------------------------+
cases: 3, passed: 3, failed: 0, incomplete: 0, not run: 0, success rate: 100%, tokens: 140, cost: $0.0000
```

`cost` shows `unknown` when a model's price is unknown. Without `--live`, the
TaskCase row is `not_run` with detail `TaskCase needs an LLM; run with
live=True`. `--fake-llm FILE` (a hidden, test-only option) takes a JSON list of
scripted `LLMResponse` objects; a list-backed file is used afresh for every run.

When neither the spec nor `HARNESS_MODEL` names a model, ToolCases still run (they
use a placeholder model name); TaskCases need a real one.

## The runner from Python

`EvalRunner(harness_factory, cases, *, runs=None, llm=None, live=False,
executor=None, judge=None)`. `await runner.run()` returns an `EvalReport`.

- `harness_factory(llm)` is called **once per run**, so every run gets a fresh
  harness. ToolCases pass a scripted client; TaskCases pass `llm` (None means the
  harness default). Any object with `async run_traced(task)` returning
  `(RunResult, Trace)` works; `Harness` does.
- A reused `llm` must answer every run. Give `FakeLLMClient` a callable script,
  not a list: a list keeps its position and run 2 would end with `error`.
- `live=False` makes every TaskCase and `rubric` check `not_run`.
- `py_assert` needs `executor`; `rubric` needs `judge`
  (`Callable[[str], LLMClient]`, called with `judge_model`).

```python
import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.core.protocols import LLMClient
from harness_builder.evals.models import EVAL_CASES
from harness_builder.evals.runner import EvalHarness, EvalRunner
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


cases = EVAL_CASES.validate_python(
    [
        {
            "kind": "tool",
            "id": "add-basic",
            "tool": "add",
            "args": {"a": 1, "b": 2},
            "check": {"type": "equals", "value": 3},
        },
        {
            "kind": "tool",
            "id": "add-wrong",
            "tool": "add",
            "args": {"a": 1, "b": 2},
            "check": {"type": "contains", "text": "4"},
        },
        {
            "kind": "task",
            "id": "ask",
            "input": "What is 1 + 2?",
            "check": {"type": "contains", "text": "3"},
        },
    ]
)


def factory(llm: LLMClient | None) -> EvalHarness:
    builder = (
        HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini"))
        .tools(add)
        .permissions([Rule("add", "allow")])
        .trace_dir(None)
    )
    if llm is not None:
        builder.llm(llm)
    return builder.build()


report = anyio.run(EvalRunner(factory, cases).run)
for case in report.cases:
    print(case.id, case.status, case.detail)
# add-basic pass None
# add-wrong fail contains check failed on '3'
# ask not_run TaskCase needs an LLM; run with live=True
print(report.totals.passed, report.totals.failed, report.totals.not_run, report.ok)
# 1 1 1 False
```

`EVAL_CASES` is a pydantic `TypeAdapter[list[EvalCase]]`. To read a spec's
cases use `harness_builder.spec.loader.load_eval_cases(spec, base_dir, source)`.

## Metrics

| Field | Meaning |
|---|---|
| `steps` | Number of loop steps in the run's trace. A ToolCase takes 2. |
| `tokens` | Input plus output tokens of the harness run. 0 for ToolCases; judge tokens excluded. |
| `cost` | Sum of run costs, or `None` when any run's price is unknown. |
| `success_rate` | Share of decided (pass or fail) runs that passed; `None` when none was decided. |
| `mean_steps` | Mean steps over decided runs. |
| totals | The per-case rates and step means averaged over decided cases, plus counts per status and summed tokens and cost. |

## Known gaps

- Only offline fake-model runs exist. The 20 percent step-reduction criterion
  (OQ-15) and any live success rate have not been measured.
- `py_assert`, Docker-runtime ToolCases and `run_code` cases have never run in a
  container.
- With `--trust-tools` in `harness validate`, a ToolCase aimed at a built-in tool
  is `incomplete` (the executor is faked). `harness eval --trust-tools` does not
  fake it; it uses the real Docker executor.
