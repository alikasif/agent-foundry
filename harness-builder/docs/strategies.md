# Strategies: ReAct and CodeAct

A **strategy** decides how a model's response becomes actions and how results go
back to it. It owns four things: the system prompt, the shape of the request, the
parsing of the response and the rendering of tool results
(`harness_builder.core.protocols.Strategy`). Two are registered: `react` and
`codeact`. For writing your own see [extending.md](extending.md#writing-a-strategy).

CodeAct needs the Docker sandbox. Read [docker.md](docker.md) for setup and
[isolation.md](isolation.md) for what the sandbox does and does not protect
against.

> **Not run live.** No live LLM run and no real-Docker run has been made. The
> behaviour below comes from the source and from offline runs with a scripted
> model and a fake executor. The `run_code` tool and the CodeAct prompt have not
> been exercised against a real container.

- [ReAct vs CodeAct](#react-vs-codeact)
- [The run_code tool](#the-run_code-tool)
- [Stateless execution](#stateless-execution)
- [FINAL](#final)
- [How CodeAct parses a response](#how-codeact-parses-a-response)
- [What the model sees](#what-the-model-sees)
- [Sub-agents: spawn_agent](#sub-agents-spawn_agent)
- [Example: a CodeAct harness](#example-a-codeact-harness)
- [Switching strategy](#switching-strategy)
- [Known gaps](#known-gaps)

## ReAct vs CodeAct

| | `react` (default) | `codeact` |
|---|---|---|
| How the model acts | Native function calls | Fenced `python` or `bash` code blocks, plus native calls for other tools |
| Tools sent as declarations | All visible tools | All visible tools **except** `run_code` |
| Needs | Nothing | The `run_code` tool (`required_tools={"run_code"}`) |
| Finishing | Plain text with no call | `FINAL(...)` in code, or plain text with no block and no call |
| Tool results | One `tool` message per call, JSON or text | Native calls: `tool` messages. Code blocks: one `user` message of `<execution>` observations |
| Output cap in the prompt context | None (up to the executor's 64 KiB per stream) | 4,000 characters per stream by default, head and tail kept |
| Extra import cost | None | Loads the sibling `codeact_agent` package on first parse (about 12 s cold) |

Select it in a spec with `strategy.name` or in code with `.strategy("codeact")`.
`run_code` can also be used **under ReAct**: the model then calls it as an
ordinary function with `{"language", "code"}` and reads the result as JSON.

`CodeActStrategy` takes one option, `output_max_chars` (default 4000, must be
> 0, else `ConfigError`):

```yaml
strategy:
  name: codeact
  options:
    output_max_chars: 2000
```

## The run_code tool

`run_code(language, code)` runs one complete program in a fresh container and
returns the result.

| Argument | Type | Notes |
|---|---|---|
| `language` | `"python"` or `"bash"` | Anything else is a `validation` error; nothing reaches Docker. |
| `code` | `str`, at least 1 character | The whole program. |

Extra keys are rejected. The tool's risk class is `code_exec` and it is marked
nondeterministic (replay returns the recorded result instead of re-running it).

Spec options (a bad value is a located `SpecError`):

| Option | Default | Notes |
|---|---|---|
| `timeout_s` | 30 | Number > 0, the per-execution limit. |
| `network` | `false` | `true` gives the container network access. See [isolation.md](isolation.md#what-is-not-blocked). |

Memory, CPU, pids and the output cap are not spec options
([docker.md](docker.md#configuration)).

```yaml
tools:
  - builtin: run_code
    options:
      timeout_s: 20
      network: false
permissions:
  rules:
    - tool: run_code
      decision: allow
```

**You must allow it.** There is no auto-allow rule for `run_code`. With the
default deny-unlisted permissions, the call is blocked until a rule such as the
one above allows it (or, interactively, an `ask` rule is approved).

### What a result looks like

Every completed execution is an observation with `ok=True`, whatever its exit
code. The outcome is in the fields (`ExecResult`):

| Field | Meaning |
|---|---|
| `exit_code` | The real exit code, or `None` after a timeout. |
| `stdout`, `stderr` | Captured text, each capped at 64 KiB by the executor. |
| `timed_out` | The timeout fired and the container was killed. |
| `truncated` | A stream was cut at the cap. |
| `oom_killed` | The container hit its memory limit. |
| `final_set`, `final_value` | Whether the code called `FINAL` and the answer. |

If the executor itself fails (Docker went away, an unsafe mount) the observation
is `ok=False` with error type `executor_error` and the hint
"The sandbox is unavailable; run `harness doctor` or answer without code." The
failure is fed back to the model; it does not crash the run.

When the harness is built, the builder binds a `DockerExecutor` to `run_code` and
runs Docker preflight once at the top level (child harnesses never preflight).
`build()` raises `DockerUnavailableError` if Docker is not ready, and the CLI
exits 3. Preflight is skipped when a custom tool runner is set (replay) or with
`.executor(..., preflight=False)`.

## Stateless execution

Each code block runs in a **fresh container and a fresh process** (design
decision U1, the recommended default; `state_mode="session"` exists as a config
field but is not implemented). Consequences:

- Variables, imports and function or class definitions do **not** carry over to
  the next block. The model must re-import and redefine what it needs.
- Files written under `/work` **do** persist for the rest of the run. Python
  modules saved to `/work` can be imported by later blocks (the entry script puts
  the working directory first on `sys.path`).
- Nothing persists after the run ends: the `/work` directory is deleted.
- Each harness run, and each sub-agent run, gets its own `/work`. A child does
  not see the parent's files.

The CodeAct system prompt tells the model all of this, so it saves intermediate
results to files.

## FINAL

`FINAL` ends the model's turn with an answer from inside code.

**Python.** `FINAL(answer)` is injected into every Python block (no import
needed). It records `str(answer)` and stops the block immediately; code after it
does not run. It is implemented with a private `BaseException`, so a user-code
`except Exception:` does not swallow it. The block exits with code 0.

```python
data = [1, 2, 3]
FINAL(f"The total is {sum(data)}")
print("never printed")
```

**Bash.** Run `hb-final "answer"`, **then `exit 0`**:

```bash
hb-final "42"
exit 0
```

`hb-final` only records the answer; it does not stop the script, so anything after
it still runs, and the script's own exit code is the process exit code. The prompt
tells the model to always follow it with `exit 0`.

How the answer reaches the loop:

1. The entry script writes the answer as a JSON string to the file named by
   `HB_FINAL_PATH`.
2. After the container has been removed, the executor reads that file, refusing
   links and reading at most the output cap. If the container could not be
   removed, FINAL is treated as unset.
3. `run_code` copies it to the observation's `final` field; the loop stops with
   reason `done` and that answer.

Details that surprise people:

- The answer is honoured **even after a timeout or a non-zero exit**, if the file
  was written before the process died. The tool does not require a clean exit.
- Each run of a block first deletes any earlier FINAL file, so a stale answer from
  a previous block cannot make a later block look final.
- If several calls are in one response, the loop executes them in order and stops
  at the first FINAL. The remaining calls get an error observation
  ("not executed: an earlier call in this step set the final answer"). An
  `on_stop` hook can still veto the answer, in which case the run continues.
- The code review recommends accepting FINAL only after a clean exit (exit 0, no
  timeout, no OOM); that change was requested but had not landed when this page was
  written.
- FINAL is a convention enforced by files the code controls; see
  [isolation.md](isolation.md#what-is-not-blocked).

The other valid way to finish under CodeAct is a response with **no code block and
no native call**: its text is the final answer.

## How CodeAct parses a response

`CodeActStrategy.parse` applies these rules in order:

1. Fenced blocks are extracted (languages lowercased; `py` becomes `python`, `sh`
   becomes `bash`; `python3` is also mapped to `python`).
2. Untagged fences and data or markup tags are **prose and ignored**: `txt`,
   `plaintext`, `output`, `console`, `log`, `json`, `yaml`, `yml`, `toml`, `xml`,
   `html`, `csv`, `markdown`, `md`, `diff`. A reply that only quotes JSON is
   therefore a final answer, not an error.
3. Any other tag that is not `python` or `bash` (for example `javascript`, `sql`)
   makes the **whole response** a `format` error, listing `python, bash`. Nothing
   runs.
4. Each remaining block becomes a `run_code` call with id `codeact_block_<i>`.
   Native tool calls from the model come after the block calls, in response order.
   A native call with no id gets `call_<i>`; a call with a blank name or duplicate
   ids is a `format` error.
5. No block and no native call: the text is the final answer. Blank text is a
   `format` error.

Block calls are **not** put in the assistant message's `tool_calls`, because the
model never emitted them and providers reject a result for a call they did not
see.

## What the model sees

The system prompt is your base prompt, then the CodeAct prompt
(`CODEACT_DOCKER_PROMPT`), then a `## Native Tools` list of every other tool. The
prompt reuses eight sections of the sibling `codeact_agent` prompt verbatim (they
are exposed as `REUSED_SECTIONS` for a drift test against the sibling prompt;
that test is owned by a later task and is not in the repo yet) and rewrites what is false inside a fresh Linux
container: it says blocks are stateless and `/work` persists, drops Git Bash and
awk, and tells the model to delegate with `spawn_agent` rather than the sibling's
`llm_query`.

After blocks run, one `user` message holds one observation per block:

```text
<execution>
<stdout>
...
</stdout>
<stderr>
...
</stderr>
<exception>
None
</exception>
</execution>
```

stdout and stderr are each cut to `output_max_chars` (head and tail, with an
omission marker); if the executor itself hit its byte cap, a note is added to
stderr. `<exception>` is `None` for a normal exit; otherwise it lists, one per
line, an `executor_error`, `timeout: the code exceeded the time limit and was
killed`, `out_of_memory: the container ran out of memory`, or `exit code N`. A
Python traceback appears in stderr.

Under ReAct the same result is one `tool` message with the JSON object of the
result fields, with no 4,000-character cap: up to 64 KiB per stream enters the
context on every call. Use a [context policy](layers.md) if that is a problem.

## Sub-agents: spawn_agent

`spawn_agent(task, profile)` delegates a self-contained task to a child harness
and returns its final answer. It is registered by `.subagents(...)` or the
`subagents` spec section. Under CodeAct it is a **native function call, never
called from inside a code block**: the sandbox has no access to the harness.

- The child does not see the parent's conversation, so the task must carry its own
  context.
- It gets a share of the remaining budget (`budget_fraction`, default 0.5) and
  runs one level deeper; the default `max_depth` is 2 and deeper spawns return a
  `depth_exceeded` error.
- Children share the parent's executor but get their own `/work` directory.
- A rule naming `spawn_agent` exactly overrides the automatic allow rule.
- Child traces are embedded in the parent trace; replay returns the recorded
  answer rather than re-running the child.

## Example: a CodeAct harness

This runs offline: a scripted model replies with one `python` block and a
`FakeExecutor` stands in for Docker, so it shows the wiring, not a real
sandbox. To use real Docker, drop `.executor(executor)` (the builder then binds a
`DockerExecutor` and preflights it).

````python
import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.builtin.run_code import run_code

fence = "`" * 3
reply = f"Sum it.\n{fence}python\nFINAL(sum(range(5)))\n{fence}"
executor = FakeExecutor([ExecResult(exit_code=0, final_set=True, final_value="10")])
harness = (
    HarnessBuilder("codeact-demo", Settings(model="openai/gpt-4o-mini"))
    .strategy("codeact")
    .tools(run_code())
    .permissions([Rule("run_code", "allow")])
    .executor(executor)  # a FakeExecutor: no Docker needed
    .llm(FakeLLMClient([LLMResponse(text=reply)]))
    .build()
)
result = anyio.run(harness.run, "Sum 0..4")
print(result.answer)  # 10
print(result.stop.reason)  # done
req = executor.requests[0]
print(req.language, req.timeout_s, req.network)  # python 30.0 False
````

The same harness as a spec (loads and builds offline with a fake executor; a real
`harness run` needs Docker, the image and `OPENROUTER_API_KEY`):

```yaml
schema_version: 1
name: codeact-demo
model:
  name: openai/gpt-4o-mini
strategy:
  name: codeact
  options:
    output_max_chars: 2000
prompts:
  system: You are a data-wrangling assistant.
tools:
  - builtin: run_code
    options:
      timeout_s: 20
      network: false
permissions:
  rules:
    - tool: run_code
      decision: allow
budget:
  max_steps: 8
```

## Switching strategy

The same tool set runs under either strategy: change only `strategy.name`, or
override it on the command line:

```text
uv run harness run demo "Sum 0..4" --strategy react
```

`--strategy` drops the spec's strategy options if the name differs and records the
override in the trace so replay matches. `codeact` fails at build with
`ConfigError` ("strategy 'codeact' requires tools ['run_code']") when `run_code`
is not among the tools.

## Known gaps

- An untagged fence is prose, so a reply like "Let me check:" plus an untagged
  code fence is taken as the final answer (open review item).

- `/work/context.txt`: the CodeAct prompt tells the model that a large input is
  stored there, but **nothing in the framework writes that file yet**. Do not rely
  on it.
- `state_mode="session"` (a long-lived container) is not implemented.
- `run_code` has no spec option for memory, CPU, pids or the output cap.
- The `<exception>` field never carries a Python exception object; tracebacks are
  in stderr.
- The CodeAct strategy and its prompt have not been tested against a live model.
