# harness-builder

A layered agent-harness framework in Python. You compose a harness from small,
swappable layers (LLM client, strategy, tools, permissions, hooks, context policy,
budget, trace), run it, and get a complete, replayable trace of what it did.

Status: milestone M3 (M1 framework core, spec loader, trace replay and diff, the
Docker sandbox, the `run_code` tool, the CodeAct strategy and `harness doctor`,
plus the harness generator `harness new`, validation, evals and the full
HarnessSpec v1). Skills are a later milestone and are not documented here yet.
**No live LLM run has been made yet, and the Docker sandbox has not been run
against a real Docker daemon yet**; everything below was exercised offline with a
scripted fake model, a fake Docker CLI and `--trust-tools`.

## Install

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```text
uv sync
```

The package depends on two sibling projects through editable path dependencies
(`../skillful_agent` and `../codeact_agent`), so they must exist next to this
directory.

### Prerequisite for code execution: Docker

Everything except `run_code` and the `codeact` strategy works without Docker. To
let a model run code you need Docker (Docker Desktop on Windows and macOS, with
Linux containers; Docker Engine on Linux) and the sandbox image:

```text
uv run harness doctor
```

`doctor` checks the `docker` CLI, the daemon and the image within 5 s, prints the
exact `docker build` command for the image if it is missing, and exits 3 if
anything fails. There is no fallback to running code on the host. The container is
a best-effort sandbox on a shared kernel, **not yet exercised against a real
daemon**; read [docs/isolation.md](docs/isolation.md) before relying on it. Setup
details: [docs/docker.md](docs/docker.md).

## Configure

Copy the example environment file and fill it in:

```text
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter (required for live runs; never needed offline). |
| `HARNESS_MODEL` | Default OpenRouter model id, for example `openai/gpt-4o-mini`. A spec's own `model` wins. |
| `HARNESS_TRACE_DIR` | Where traces are written. Default `.harness/traces`. |
| `HARNESS_SKILLS_ROOT` | Reserved for the skills layer (later milestone). |
| `HARNESS_DOCKER_IMAGE` | Sandbox image tag. Default: the content-hash tag `harness-builder-sandbox:<hash>` that `harness doctor` prints. |

Real environment variables take precedence over `.env`. The file is read from the
current working directory, is never copied into `os.environ`, and empty values
count as unset. Secrets found in the environment and `.env` are redacted from
traces ([docs/traces.md](docs/traces.md#redaction)).

## Quickstart: ReAct with a fake LLM (no key, no network)

`FakeLLMClient` plays back scripted responses, so this runs offline. The harness
uses the default `react` strategy with one tool.

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
    .strategy("react")
    .system_prompt("You are a calculator.")
    .tools(add)
    .permissions([Rule("add", "allow")])  # unlisted tools are denied by default
    .budget(max_steps=5)
    .llm(FakeLLMClient(script))
    .trace_dir(".harness/traces")
    .build()
)
result = anyio.run(harness.run, "What is 2 + 3?")
print(result.answer)  # 2 + 3 = 5
print(result.stop)  # reason='done' ...
print(result.trace_path)  # .harness/traces/<run_id>.json
```

For a live run, drop `.llm(...)` (the default is `AdkLiteLlmClient` over
OpenRouter) and set `OPENROUTER_API_KEY` and a model.

Two things to know:

- Tools are **denied unless a rule allows them** (ASSUMED default). The
  `Rule("add", "allow")` line is required.
- `harness.run` returns a `RunResult` (`answer`, `stop`, `usage`, `trace_path`).
  Failures of tools, permissions and hooks are fed back to the model rather than
  raised; see [docs/layers.md](docs/layers.md#the-step-loop).

## Quickstart: the CLI, from a spec

A harness can be described declaratively. Create `demo/harness.yaml`:

```yaml
schema_version: 1
name: demo
model:
  name: openai/gpt-4o-mini
strategy:
  name: react
prompts:
  system: You are a concise assistant.
budget:
  max_steps: 5
```

Run it, replay the recorded trace and inspect it:

```text
uv run harness run demo "Say hello" --trace demo/trace.json
uv run harness replay demo/trace.json
uv run harness trace show demo/trace.json
uv run harness diff demo/trace.json other-trace.json
```

`harness run` needs `OPENROUTER_API_KEY` for a real model. To try the CLI without
a key, the hidden, test-only option `--fake-llm FILE` takes a JSON list of
scripted `LLMResponse` objects, for example `[{"text": "Hello"}]`. It is meant
for tests and smoke runs and may change.

Commands and exit codes for `run`, `replay`, `diff` and `trace` are listed in
[docs/traces.md](docs/traces.md#cli-reference). Spec fields: `schema_version`
(required, `1`), `name`, `description`, `model`, `strategy`, `prompts`, `tools`
(built-in or module tools), `context`, `permissions`, `budget`, `hooks`,
`subagents`, `sandbox`, `skills` and `evals`. Unknown keys are rejected with a
located error such as `harness.yaml: budget.max_stepz: Extra inputs are not
permitted`. The full reference is [docs/spec.md](docs/spec.md).

## Generate, validate and evaluate a harness

`harness new` asks a model to write a harness directory (spec, tool modules,
eval cases, README), validates it in stages, repairs it a bounded number of
times and writes it with a validation report. It needs a model
(`HARNESS_MODEL`, `OPENROUTER_API_KEY`) and, for generated tools, Docker.

```text
uv run harness new "Count the words in a text" --non-interactive
uv run harness validate harnesses/count-the-words-in-a-text
uv run harness eval harnesses/count-the-words-in-a-text
uv run harness run harnesses/count-the-words-in-a-text "How many words in: a b c?"
```

| Command | Exit 0 | Exit 1 | Exit 2 | Exit 3 |
|---|---|---|---|---|
| `new` | validation passed | validation failed (still written, marked failed) | usage or config error | validation incomplete (for example no Docker) |
| `validate` | passed | failed | usage error | incomplete |
| `eval` | no case failed | a case failed | usage or config error | a case is incomplete |

Generated tools run only in Docker. `--trust-tools` (on `validate`, `eval` and
`run`) runs hand-checked `runtime: host` tools on the host instead, and
`--allow-failed` runs a harness whose validation status is `fail`. `harness
new --live` and `harness validate --live` also run TaskCases and rubric checks
with the real model. Details: [docs/generator.md](docs/generator.md) (stages,
statuses, repair loop, output layout) and [docs/evals.md](docs/evals.md) (case
and check formats).

## Documentation

- [docs/layers.md](docs/layers.md): each layer, its protocol, default
  implementation and test double; the step loop; defaults.
- [docs/extending.md](docs/extending.md): write a tool, a hook, a context policy
  or a strategy; what affects the config hash.
- [docs/traces.md](docs/traces.md): trace schema v1, redaction, replay, diff and
  the CLI.
- [docs/docker.md](docs/docker.md): Docker prerequisite, `harness doctor`, building
  the image, configuration, cleanup.
- [docs/isolation.md](docs/isolation.md): what the sandbox blocks and what it does
  not, Windows notes, latency.
- [docs/strategies.md](docs/strategies.md): ReAct vs CodeAct, `run_code`,
  stateless execution, `FINAL`, `spawn_agent`.
- [docs/spec.md](docs/spec.md): HarnessSpec v1 field reference, versioning,
  module tools, `runtime: docker` vs `host`, permission defaults.
- [docs/generator.md](docs/generator.md): `harness new`, `validate` and `run` on
  generated harnesses, validation stages, statuses, repair loop, `--live`,
  `--trust-tools`, output layout.
- [docs/evals.md](docs/evals.md): ToolCase and TaskCase, checks, the runner and
  `harness eval`.

## Known limitations (M1 to M3)

- No live OpenRouter run has been made; `AdkLiteLlmClient` is tested against a
  stubbed ADK only.
- A spec can name built-in tools and, since M3, module tools and module hooks in
  the harness directory. Module tools run in Docker by default; `runtime: host`
  tools and module hooks need `--trust-tools`. There are no built-in hooks yet,
  and custom approvers are code-only.
- The generator, validation and evals have never run against a live model or a
  real Docker daemon. Without Docker, a harness with Docker tools validates as
  `incomplete` (exit 3).
- `harness run` does not apply the safety screen's flagged-tool `ask` rules to a
  generated harness, and `harness replay` cannot rebuild a harness that has
  module tools (no harness-directory option); see
  [docs/spec.md](docs/spec.md#known-gaps).
- Docker has not been exercised against a real daemon: the sandbox, `run_code`
  and CodeAct are tested with fakes only, and the real-Docker isolation tests
  (`tests/docker`, 25 tests) exist but have never run (all skipped). Isolation is best effort
  ([docs/isolation.md](docs/isolation.md)).
- `/work/context.txt`, mentioned in the CodeAct prompt for large inputs, is not
  written by anything yet.
- Defaults marked **ASSUMED** in the docs (for example `max_steps=10`,
  deny-unlisted permissions, sub-agent depth 2) are design placeholders awaiting
  user confirmation.

## Development

```text
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyrefly check
```

Tests marked `docker` or `live` are skipped unless Docker is available or
`HARNESS_LIVE=1` is set.
