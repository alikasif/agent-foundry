# The harness generator

`harness new` turns a description into a **harness directory**: a
[HarnessSpec](spec.md), optional tool modules and eval cases, a README and a
validation report. The generator is itself a harness built with this framework;
a deterministic validation pipeline then checks what it wrote, and a bounded
repair loop feeds failures back to it.

> **Not run live.** No live LLM run and no real-Docker run has been made. The
> generator and the pipeline were exercised offline with a scripted model, and
> generated Docker tools were never executed in a container. On a machine
> without Docker, `harness new` and `harness validate` of a harness with Docker
> tools end `incomplete` (exit 3). The examples below were run that way.

- [Commands](#commands)
- [harness new](#harness-new)
- [Validation stages](#validation-stages)
- [Statuses and exit codes](#statuses-and-exit-codes)
- [The repair loop](#the-repair-loop)
- [The safety screen](#the-safety-screen)
- [Trust and the failed-harness gate](#trust-and-the-failed-harness-gate)
- [Output layout](#output-layout)
- [From Python](#from-python)
- [Known gaps](#known-gaps)

## Commands

| Command | Purpose |
|---|---|
| `uv run harness new DESCRIPTION` | Generate, validate, repair and write a harness. |
| `uv run harness validate TARGET` | Run the validation pipeline on a harness directory and print the report. |
| `uv run harness eval TARGET` | Run the harness's eval cases ([evals.md](evals.md)). |
| `uv run harness run TARGET TASK` | Run a harness; refuses a harness whose status is `fail`. |

`TARGET` is a harness directory (or, for `run` and `eval`, a spec file).

## harness new

```text
uv run harness new "Count the words in a text" --non-interactive
uv run harness new "Summarise a CSV file" -o my-harnesses/csv --max-attempts 2
```

| Option | Default | Meaning |
|---|---|---|
| `DESCRIPTION` | required | What the harness should do. |
| `-o`, `--output DIRECTORY` | `./harnesses/<slug>` | Output directory. The slug is the first six words of the description, lowercased, dash-joined, at most 40 characters. |
| `--non-interactive` | off | Never ask clarifying questions; assumptions go in the README. |
| `--max-attempts N` | 3 | Generator runs, **the first draft included**. Must be at least 1. |
| `--live` | off | Also run TaskCases and Rubric checks with the real LLM during validation. |

Things to know:

- The output directory must not exist or must be empty. Otherwise the command
  fails with a `ConfigError` and exits 2.
- A model is required: set `HARNESS_MODEL` and `OPENROUTER_API_KEY` (see
  the [README](../README.md#configure)). Without a model `new` exits 2 with
  `model not set; call .model(...) or set HARNESS_MODEL`. The generator and the
  generated harness are separate models: the generated harness's spec names its
  own.
- Interactive mode (the default) lets the generator ask up to 5 clarifying
  questions on stderr. With `--non-interactive` the question tool is
  auto-denied.
- The CLI takes a description only. `GenerationRequest` also has `examples`,
  `constraints` and `allowed_tools`; those are reachable from
  [Python](#from-python), not from the CLI.
- `new` has no `--trust-tools`. Freshly generated code never runs on the host
  during generation.
- On success it prints three lines and exits with the validation status code:

```text
status: pass after 1 attempt(s)
harness: <absolute output directory>
report: <absolute output directory>/reports/validation.md
```

A failed harness is **still written**, marked `fail`, with a banner in its
README. It is never reported as passing.

### What the generator can do

The generator has six tools and no code execution:

| Tool | Behaviour |
|---|---|
| `write_file(path, content)` | Write one file in a staging directory (`<output>.staging/`), at most 256 KiB. |
| `read_file(path)` | Read a file from staging. |
| `list_builtin_tools()` | The built-in tools a spec may reference (filtered by `allowed_tools`, advisory only). |
| `get_spec_schema()` | The HarnessSpec JSON schema. |
| `ask_user(question)` | Interactive requests only; the 6th call is an error. |
| `submit(summary)` | Ends the run. |

Paths are confined to staging: absolute paths, drives, `..`, backslashes, names
ending in `.` or a space, and links that leave staging are rejected. It cannot
write `reports/`, `.harness-meta.json` or `tools.schema.json` (the pipeline owns
them), in any letter case.

## Validation stages

`harness validate` and the generator both run the same `ValidationPipeline`.
Stages run in order. A stage that fails or cannot finish decides what happens to
the rest:

| Stage | Name | Needs Docker | What it does |
|---|---|---|---|
| A | static checks | no | Loads the spec and eval cases (located errors). Every module ref must resolve inside the directory, name a top-level function whose parameters are type-hinted (no `ctx`, `*args`, `**kwargs`, positional-only), and have at least one `examples` entry. Runs `ruff check` (rules `E9,F63,F7,F82`: syntax errors and undefined names) and `pyrefly check` on the referenced modules, and the [safety screen](#the-safety-screen). `runtime: host` tools and module hooks are rejected without `--trust-tools`. Never imports or runs harness code. |
| B0 | schema extraction | yes, if any `runtime: docker` tool | Runs `hb_entry.py schema` in a container for each Docker tool and writes `tools.schema.json`. `not_run` when there are no Docker tools. |
| B | smoke run | for Docker tools or `run_code` | Builds the harness from the directory and drives it with a scripted model that calls every module tool once with its `examples[0]`, then finishes. Passes only if the run stops with `done`, every call has an `ok` observation and the trace round-trips through JSON. |
| C | tool evals | for Docker tools, `run_code` and `py_assert` checks | Runs the ToolCases in `evals` ([evals.md](evals.md)). TaskCases run only with `--live`; otherwise they are `not_run`. `not_run` when there are no cases. |

If stage A fails, B0, B and C are `not_run`. Real output for a tool with a
missing type hint, validated with `--trust-tools`:

```text
$ uv run harness validate bad --trust-tools
# Validation report: word-counter

**Status: FAIL** (exit code 1)

- Attempts: 1
- Tool runtime: trusted host tools
- Live LLM: no

## Stages

| Stage | Name | Status | Detail |
|---|---|---|---|
| A | static checks | fail | 1 problem(s) |
| B0 | schema extraction | not_run | stage A failed |
| B | smoke run | not_run | stage A failed |
| C | tool evals | not_run | stage A failed |

### Stage A problems

- harness.yaml: tools[0].function: parameter 'text' needs a type hint

## Safety findings (tools get an `ask` rule)

- `tools/text_tools.py` line 1 [subprocess] import subprocess (tools: count_words)
```

`validate` prints this report on stdout. **It does not write `reports/`**
(it writes only `tools.schema.json`, from stage B0). When a
`.harness-meta.json` already exists it updates its `status` to the new verdict,
so a harness that now fails stops being runnable. A `fail` to `pass` upgrade is
**refused** when the run used `--trust-tools` (a host run never clears a `fail`),
or when it left cases `not_run` while the run that recorded the `fail` ran them
all. The meta stores `all_cases_ran` with each verdict (a non-live `harness new`
with TaskCases stores `false`, so a non-live Docker `validate` can clear it, but
a `--trust-tools` one cannot). Only a stored `false` counts as "skipped cases";
a missing or any other value counts as "all ran", so legacy and malformed metas
fail closed (they need a run with every case executed, `--live` when TaskCases
exist). A refused upgrade leaves the `fail` in place (use `--allow-failed` to run
it). `trusted_tools` is informational and follows the latest verdict. An unrecognised `status` counts as a `fail`. A hand-written harness has no meta file
and gets none.

### Options

| Option | Effect |
|---|---|
| `--live` | Run TaskCases and Rubric checks with the real LLM (the default client, built from `HARNESS_MODEL`, or the spec's model when that is unset). One client serves every TaskCase run. |
| `--trust-tools` | Run **every** module tool on the host instead of in Docker, and give `run_code` a fake executor. For hand-checked code. A ToolCase that targets a built-in tool (such as `run_code`) is then `incomplete`, because that tool is faked. |

Without Docker, a harness that needs it (a Docker module tool, or the
`run_code` built-in, which binds the sandbox executor at build time) ends
`incomplete`. For a Docker module tool, stage B0 reports
`Docker is unavailable; generated code runs only in Docker`, and B and C are
`incomplete` with `stage B0 could not finish`. No fake executor stands in for
generated tools. Run `uv run harness doctor` to diagnose
([docker.md](docker.md)).

## Statuses and exit codes

Stage and report statuses:

| Status | Meaning |
|---|---|
| `pass` | Every stage that ran passed, and none was blocked. |
| `fail` | At least one stage failed. Outranks `incomplete`. |
| `incomplete` | No stage failed, but one could not finish: Docker or a linter is unavailable, or a case could not be decided. Never counted as a pass. |
| `not_run` | (Stage and case level only.) Skipped by design: no Docker tools, no eval cases, or a TaskCase without `--live`. |

The report status is `fail` if any stage is `fail`, else `incomplete` if any
stage is `incomplete`, else `pass`. A report is `pass` even when stage C is
`not_run`.

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `new` | status `pass` | status `fail` | usage or config error (non-empty `-o`, no model, no API key) | status `incomplete` |
| `validate` | `pass` | `fail` | usage error, unreadable path | `incomplete` |
| `eval` | no case failed or incomplete | a case failed | usage or config error (including an untrusted `runtime: host` tool) | a case is `incomplete` and none failed |
| `run` | stopped `done` | stopped for any other reason | config or spec error, including a `fail` harness without `--allow-failed` | Docker unavailable, or an `incomplete` harness whose `tools.schema.json` is missing (run `harness validate` first) |

## The repair loop

`harness new` repairs only `fail`:

1. The generator drafts into `<output>.staging/`.
2. The pipeline validates staging.
3. On `fail`, another generator run starts. Its task is the original request
   plus `Attempt N failed validation...` and the Markdown report. The staged
   files persist, so the generator reads and fixes them with `read_file`. Each
   repair is a **fresh run, not a new turn** in the same conversation.
4. The loop ends on `pass`, on `incomplete`, when `--max-attempts` runs have
   been used (the draft counts as one), or when a generator run stops with
   `budget` or `error`. Validation still runs on whatever was written.

`incomplete` is never repaired, because a missing Docker daemon or linter is not
something the generator can fix. One budget covers the draft and every repair
(default 60 steps, no cost cap).

## The safety screen

Stage A parses the Python the harness contains (never importing it) and flags
risky constructs in five categories:

| Category | Flags |
|---|---|
| `network` | imports of `socket`, `ssl`, `urllib`, `urllib3`, `requests`, `httpx`, `aiohttp`, `http.client`, `http.server`, `ftplib`, `smtplib`, `poplib`, `imaplib`, `telnetlib`, `xmlrpc`, `websocket(s)` (also through dynamic imports), `asyncio.open_connection` / `open_unix_connection` / `start_server` |
| `subprocess` | imports of `subprocess`, `pty`, `multiprocessing`, `posix`, `nt`, `_posixsubprocess`; `os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `os.fork*`; `asyncio.create_subprocess_exec` / `_shell` |
| `delete` | `os.remove`, `os.unlink`, `os.rmdir`, `os.removedirs`, `shutil.rmtree`, `.unlink()`, `.rmdir()` |
| `dynamic_exec` | `eval`, `exec`, `compile`, `__import__`, `importlib.import_module`, the `importlib.util` / `importlib.machinery` loaders, `.exec_module()` / `.load_module()`, `runpy`, `pickle` / `marshal` loads, `types.FunctionType`, `ctypes`; the import-system hooks (`sys.meta_path`, `sys.path_hooks`, `sys.path_importer_cache`, `importlib.machinery.*_SUFFIXES`); **any reference to `sys.path`** (reads, aliases, `from sys import path`, mutation); any import of `site`, `sitecustomize` or `usercustomize`; relative imports and any binding of `__path__`, `__package__` or `__spec__`; imports of stdlib modules that run a string or file (`timeit`, `code`, `codeop`, `pdb`, `bdb`, `cProfile`, `profile`, `trace`, `doctest`, `zipimport`, `importlib._bootstrap*`, `_frozen_importlib`, `_frozen_importlib_external`, `importlib.abc`, `logging.config`, `webbrowser`, `_ctypes`); any reference to `importlib.machinery` `SourceFileLoader`, `SourcelessFileLoader`, `ExtensionFileLoader` or `FileFinder`; calls to `.get_code()`, `.source_to_code()`, `.get_source()`, `.load_extension()`, `typing.ForwardRef`, `pkgutil.resolve_name`, `pydoc.locate` / `pipepager`, `breakpoint()` and `sys.breakpointhook()` (they run `.pdbrc`) |
| `secret` | key-like literals, secret-looking assignments, high-entropy strings |

**Scope.** `ContainerTool` mounts the whole harness directory read-only at
`/tools`, so the screen covers what is mounted: every `*.py` at any depth, in
hidden directories and in `tests/`, `skills/` and `evals/` too. Only the
top-level `reports/` directory (written by the pipeline) is skipped. Every
`.py` file is screened, named tool and hook modules included, and a finding in
any of them flags **every** module tool of the harness: a tool can run another
file through a relative import, and the loader cannot tell which tool loads
which file (T062 MJ1). The screen also **fails closed**: any `.pth`, `.pyc`, `.pyo`, `.so`, `.pyd`, `.zip`, `.egg` or `.whl`
file anywhere in the tree (none can be screened, and each can execute) flags
every module tool. A file that cannot be read or parsed, or a directory that
cannot be listed, does the same. Running `python` or `pytest` inside a harness
directory leaves `__pycache__/*.pyc` files, which flag every tool until you
delete them.

It is still a best-effort heuristic, not a proof that stored code cannot run: it cannot see through `getattr`/`setattr` string
indirection or data flow, and other deserialisers (`shelve`, `dill`) are not
reported. Residual string indirection is advisory only: aliasing a builtin
to a variable (`e = exec; e(s)`, `bp = breakpoint`), `getattr`,
`vars(builtins)["exec"]`, building a function from a code object with
`type(f)(code, {})()`, and `typing.get_type_hints` / `inspect.get_annotations(eval_str=True)`
over string annotations read from a file. The container
(`--network none`, read-only mount) is the boundary;
the screen only decides what asks for approval.

A finding does not fail validation. It lists the affected tools in
`flagged_tools`. `harness run` and `harness eval` apply the same rule: the
loader re-screens every module tool each time it builds the harness
(`builder_from_spec`), so each flagged tool gets an `ask` rule ahead of the
spec's own rules **even when `reports/` was deleted**. A builder created from
code with `flagged_tools=` adds its names to that list.

`run` and `eval` both use the default approver: it prompts on a terminal when
stdin is a TTY and **denies otherwise**. So in `harness eval` without a TTY (CI,
a pipe) a flagged ToolCase fails because the tool is denied, and with a TTY it
asks; either way a flagged tool never executes silently.

## Trust and the failed-harness gate

- `harness run` and `harness eval` read `.harness-meta.json` in a harness
  directory. Status `pass` and `incomplete` run. `fail`, any other value or an
  unreadable file stops with exit 2:

  ```text
  error: .harness-meta.json: this harness failed validation (status 'fail'); see reports/validation.md, or pass --allow-failed (allow_failed=True) to run it anyway
  ```

- `--allow-failed` (on `run` and `eval`) bypasses that check.
- `--trust-tools` (on `run`, `eval` and `validate`) allows `runtime: host`
  tools and module hooks, which run on the host. On `run` and `eval` it does
  **not** move `runtime: docker` tools to the host; on `validate` it does.
- A harness without `.harness-meta.json` (hand-written) has no gate.

## Output layout

```text
<output>/
  harness.yaml
  README.md
  <tool modules and eval files the generator wrote>
  tools.schema.json        (when there are Docker tools)
  reports/validation.md
  reports/validation.json
  .harness-meta.json
```

`.harness-meta.json` (a real one from an offline run):

```json
{
  "status": "pass",
  "attempts": 1,
  "generator": "harness-generator",
  "generator_version": "0.1.0",
  "model": "openai/gpt-4o-mini",
  "generator_stop": "done",
  "trusted_tools": false,
  "all_cases_ran": true,
  "created_at": "2026-11-21T13:12:11+00:00",
  "request": {
    "description": "A greeter",
    "examples": [],
    "constraints": [],
    "allowed_tools": [],
    "interactive": false
  }
}
```

The README is the generator's own, or a template, with a `FAILED VALIDATION` or
`VALIDATION INCOMPLETE` banner on top when the status is not `pass`. The harness
is moved into place with one rename from staging, so a crash never leaves a
half-written output directory. A leftover `<output>.staging/` is deleted on the
next run only when it carries the `.hb-staging` ownership marker; an unmarked
directory of that name is not ours, so the run stops with exit 2 and leaves it
alone.

Whether to commit generated harnesses to git is undecided (U7). `harnesses/` is
the default output location.

## From Python

Validate a directory. This needs no Docker because `trust_tools=True` runs the
tool on the host and the spec has no `run_code`:

```python
import tempfile
from pathlib import Path

import anyio

from harness_builder import Settings
from harness_builder.generator.validate import ValidationPipeline

SPEC = """\
schema_version: 1
name: word-counter
model: {name: openai/gpt-4o-mini}
tools:
  - module: tools/text_tools.py
    function: count_words
    examples: [{text: "one two three"}]
permissions:
  rules: [{tool: count_words, decision: allow}]
evals:
  - {kind: tool, id: three, tool: count_words, args: {text: "a b c"},
     check: {type: equals, value: 3}}
"""
TOOL = '''\
def count_words(text: str) -> int:
    """Count the whitespace-separated words in text."""
    return len(text.split())
'''

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "tools").mkdir()
    (root / "harness.yaml").write_text(SPEC, encoding="utf-8")
    (root / "tools" / "text_tools.py").write_text(TOOL, encoding="utf-8")
    pipeline = ValidationPipeline(trust_tools=True, settings=Settings())
    report = anyio.run(pipeline.run, root)
    print(report.status, report.exit_code)  # pass 0
    for stage in report.stages:
        print(stage.id, stage.status, stage.detail)
```

Run the whole generator with a scripted model (a live run swaps the fake for the
default client):

```python
import tempfile
from pathlib import Path

import anyio

from harness_builder import Settings
from harness_builder.core.types import ToolCall
from harness_builder.generator.pipeline import GeneratorPipeline
from harness_builder.generator.request import GenerationRequest
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse

SPEC = (
    "schema_version: 1\nname: greeter\nmodel: {name: openai/gpt-4o-mini}\n"
    "prompts: {system: Greet the user.}\n"
)
write = ToolCall(
    id="w1", name="write_file", args={"path": "harness.yaml", "content": SPEC}
)
submit = ToolCall(id="s1", name="submit", args={"summary": "ok"})
script = [
    LLMResponse(tool_calls=[write]),
    LLMResponse(tool_calls=[submit]),
    LLMResponse(text="done"),
]
settings = Settings(model="openai/gpt-4o-mini")
pipeline = GeneratorPipeline(
    llm=FakeLLMClient(script),
    settings=settings,
    validator=ValidationPipeline(settings=settings),
    max_attempts=2,
)
request = GenerationRequest(description="A greeter", interactive=False)
with tempfile.TemporaryDirectory() as tmp:
    result = anyio.run(pipeline.run, request, Path(tmp) / "greeter")
    print(result.status, result.attempts, result.exit_code)  # pass 1 0
    print(sorted(p.name for p in result.output_dir.iterdir()))
```

`GeneratorPipeline.run(request, out)` returns a `GenerationResult`
(`output_dir`, `status`, `attempts`, `report`, `generator_stop`, `exit_code`).
It raises `ConfigError` when `out` is a non-empty directory or the generator
harness does not build.

## Known gaps

- Nothing here has run against a live LLM or a real Docker daemon.
- `harness validate` does not rewrite `reports/`. It only updates `status` in an
  existing `.harness-meta.json`, so the failed-harness gate follows the latest
  verdict (T046 mn5); a `fail` is never cleared by a `--trust-tools` run, nor by a run with `not_run` cases when the failing run ran them all (T062 MJ3, R2-MJ1).
- `--live` with `validate` reuses one client for all TaskCase runs, so a
  list-backed fake script works only for single-run cases.
- `allowed_tools` is advisory: the pipeline does not reject a spec that
  references another built-in.
- The CLI cannot pass `examples`, `constraints` or `allowed_tools` to
  `harness new`.
- Validation status `incomplete` after generation is runnable, by decision
  (T048 mn4): it never means "unvalidated code runs on the host". A
  `runtime: docker` tool still needs Docker and `tools.schema.json`, so an
  incomplete harness stays sandboxed. One case is weaker: with Docker up and a
  missing linter, stages B0, B and C run but pyrefly did not.
