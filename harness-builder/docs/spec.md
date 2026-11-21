# HarnessSpec v1

A **HarnessSpec** describes a harness declaratively, in YAML or JSON. The CLI
(`harness run`, `harness validate`, `harness eval`) and
`HarnessBuilder.from_spec` turn it into the same builder calls you would write
by hand, so a spec-built harness has the same layers, trace and config hash as a
code-built one. Generated harnesses (see [generator.md](generator.md)) are specs
plus module files.

> **Not run live.** The examples below were run offline: validation and evals
> with `--trust-tools` and a scripted fake model. No live LLM run and no
> real-Docker run has been made, so `runtime: docker` tools have never executed
> in a container. See [isolation.md](isolation.md).

- [Files and layout](#files-and-layout)
- [Versioning](#versioning)
- [Field reference](#field-reference)
- [Module tools](#module-tools)
- [Runtime: docker vs host](#runtime-docker-vs-host)
- [Permissions defaults](#permissions-defaults)
- [Hooks](#hooks)
- [Sandbox, skills and evals](#sandbox-skills-and-evals)
- [Errors](#errors)
- [Loading a spec from Python](#loading-a-spec-from-python)
- [Known gaps](#known-gaps)

## Files and layout

A spec is one file. When you pass a directory, the first of `harness.yaml`,
`harness.yml`, `harness.json` is used. Relative paths inside the spec
(`prompts.system.file`, `tools[].module`, `hooks[].module`, `evals.file`,
`skills.root`) are resolved against the spec's directory and must stay inside it.

A harness directory, as `harness new` writes it:

```text
word-counter/
  harness.yaml            the spec
  README.md               usage, assumptions, validation banner
  tools/text_tools.py     module tools (optional)
  evals/cases.yaml        eval cases (optional, see evals.md)
  tools.schema.json       tool schemas written by validation stage B0
  .harness-meta.json      generator metadata and validation status
  reports/validation.md   last generation-time validation report
  reports/validation.json
```

A complete, small spec:

```yaml
schema_version: 1
name: word-counter
description: Counts words in a text.
model:
  name: openai/gpt-4o-mini
strategy:
  name: react
prompts:
  system: You count words. Use the count_words tool.
tools:
  - module: tools/text_tools.py
    function: count_words
    examples:
      - {text: "one two three"}
permissions:
  default: deny
  rules:
    - {tool: count_words, decision: allow}
budget:
  max_steps: 5
evals:
  file: evals/cases.yaml
```

## Versioning

- `schema_version` is required and must be `1`. Any other value is rejected.
- Every field added after the first M1 release is **optional**, so older specs
  load unchanged and the version stays `1`. A future incompatible change would
  bump the number; there is no migration tooling yet.
- Every section forbids unknown keys, so a typo is a located error rather than a
  silent no-op.
- A harness that does not use the later optional fields (`sandbox`, skills,
  module tools) keeps the config hash it had before they existed (FR-016, see
  [traces.md](traces.md)); this is pinned by tests.

## Field reference

All sections except `schema_version` and `name` are optional.

| Field | Type | Default | Notes |
|---|---|---|---|
| `schema_version` | `1` | required | Only `1` is supported. |
| `name` | string, at least 1 char | required | Also the harness name in traces. |
| `description` | string | `""` | |
| `model` | `{name, temperature?, max_output_tokens?}` | none | None means `HARNESS_MODEL`. A spec's model wins over `HARNESS_MODEL`. |
| `strategy` | `{name, options}` | `{name: react, options: {}}` | Registered names: `react`, `codeact` ([strategies.md](strategies.md)). |
| `prompts.system` | string, or `{file: <path>}` | `""` | A file is read at load; snapshots inline it. |
| `tools` | list of tool refs | `[]` | [Built-in or module](#module-tools). Names must be unique. |
| `context` | `{policy, max_tokens, ...}` | none | See below. |
| `permissions` | `{default, rules}` | `{default: deny, rules: []}` | [Defaults](#permissions-defaults). |
| `budget` | see below | `max_steps: 10` | |
| `hooks` | list of hook refs | `[]` | [Hooks](#hooks). |
| `subagents` | `{max_depth, profiles}` | none | Adds the `spawn_agent` tool. |
| `sandbox` | see below | none | Limits for `run_code` and Docker tools. |
| `skills` | see below | none | Skills extension. |
| `evals` | `{file}` or a list of cases | none | [evals.md](evals.md). |

**`context`** (a closed set of three policies in v1):

| Key | Applies to | Constraint |
|---|---|---|
| `policy` | all | `truncate`, `summarize` or `by_reference` (required) |
| `max_tokens` | all | integer, at least 1 (required) |
| `keep_last` | all | integer, at least 0 |
| `ref_threshold_chars`, `preview_chars` | `by_reference` only | at least 1 / at least 0 |
| `model` | `summarize` only | summarizer model name |

`policy: by_reference` implies the `deref` tool; do not list it under `tools`.
`deref` and `spawn_agent` are implied tools: listing them is an error that
names the section to add instead.

**`budget`:** `max_steps` (integer, at least 1, default 10),
`max_input_tokens`, `max_output_tokens` (integer, at least 1, unset means
unlimited), `max_cost_usd` (at least 0). A cost limit with a model whose price is
unknown is a build-time `ConfigError`.

**`subagents`:** `max_depth` (at least 1, default 2) and `profiles`, a map of
name to `{description, model, strategy, strategy_options, system_prompt, tools,
budget_fraction (0 to 1, default 0.5), permissions}`. A profile can only narrow
what the parent allows. With no profiles there is one inheriting default.

**Rules:** `{tool, decision, args?, rule_id?}`. `tool` and the values in `args`
are case-sensitive globs, and `*` also matches `/` and `..`. `decision` is
`allow`, `deny` or `ask`. First match wins.

## Module tools

A `tools` entry is one of two shapes, told apart by its keys:

```yaml
tools:
  - builtin: run_code            # a built-in tool, by name
    options: {timeout_s: 20}
  - module: tools/text_tools.py  # a function in a harness file
    function: count_words
    examples:
      - {text: "one two three"}
    runtime: docker              # default; or host
```

Built-in names in this release: `run_code` (options `timeout_s`, `network`).
There are no built-in hooks yet.

### Module tool rules

- `module` is a relative POSIX path to a `.py` file inside the harness directory.
  Absolute paths, drives, `..`, `\` and `:` are rejected, as are links that leave
  the directory.
- `function` must be a top-level function in that module. The last definition of
  that name wins.
- Every parameter needs a **type hint**. `ctx`, `*args`, `**kwargs` and
  positional-only parameters are refused (a generated tool has no run context).
  A single parameter annotated with a pydantic `BaseModel` is the explicit-model
  form: it receives the validated instance.
- The tool description is the function docstring. The arguments schema is the
  pydantic model derived from the signature, with unknown keys forbidden.
- The return value must be JSON-serialisable (anything else is passed through
  `str`). `async def` is awaited.
- `examples` is a list of argument sets. The spec accepts an empty list, but
  **validation requires at least one**: the smoke run (stage B) calls the tool
  with `examples[0]`.
- Invalid arguments come back to the model as a `validation` error and never
  reach the function body.

```python
from pydantic import BaseModel


class Query(BaseModel):
    text: str
    lowercase: bool = False


def count_words(query: Query) -> int:
    """Count the whitespace-separated words in the text."""
    text = query.text.lower() if query.lowercase else query.text
    return len(text.split())
```

## Runtime: docker vs host

| | `runtime: docker` (default) | `runtime: host` |
|---|---|---|
| Where the function runs | A fresh container per call; the harness directory is mounted read-only at `/tools` | In the harness process |
| Host imports the module? | Never. Only the container does | Yes, at build time (top-level code runs) |
| Schema source | `tools.schema.json`, written by validation stage B0 | Derived in-process |
| Needs trust flag? | No | Yes: `--trust-tools` (CLI) or `trust_host_tools=True` |
| Without Docker | Build fails with `DockerUnavailableError` (CLI exit 3) | Works |

Generated code is untrusted, so `docker` is the default. `host` exists for
hand-checked code, and validation rejects it unless you pass `--trust-tools`.

- A `docker` tool needs a `tools.schema.json` entry before the harness can be
  built. Run `harness validate <dir>` (or `harness new`) to write it. A missing
  or invalid file is a `ConfigError` whose message says to run `harness validate`.
- A `runtime: host` tool without the trust flag fails at load:
  `harness.yaml: tools[0].runtime: 'host' runs harness code outside Docker; pass
  --trust-tools (trust_host_tools=True) or use runtime 'docker'`. This was
  verified with `harness eval` on a `host` harness.
- Container tools take `timeout_s` and `network` from the spec's `sandbox`
  section, or the defaults (30 s, no network).
- Container tools cannot end the run: `FINAL` is never read from a tool container.

## Permissions defaults

- `permissions.default` is `deny`: **a tool is denied unless a rule allows it.**
  Every spec that uses a tool needs a rule such as
  `{tool: count_words, decision: allow}`.
- `ask` hands the call to the approver. The default approver prompts on a
  terminal when stdin is a TTY and denies otherwise. The generator uses
  auto-deny, and validation approves `ask` decisions so the smoke run can
  proceed.
- When the safety screen flags a tool (see [generator.md](generator.md#the-safety-screen)),
  builders created with `flagged_tools=` get an `ask` rule for it ahead of your
  rules. **`harness run` does not pass flagged tools today**; see
  [Known gaps](#known-gaps).
- The defaults marked ASSUMED in the design (deny-unlisted, sub-agent depth 2)
  are placeholders awaiting confirmation.

## Hooks

```yaml
hooks:
  - builtin: <name>          # none are registered in this release
    event: post_tool
  - module: hooks/audit.py
    function: audit
    event: pre_tool          # pre_tool, post_tool or on_stop
    priority: 0
```

Hooks are host callables: a module hook always needs `--trust-tools` and has no
container mode. Stage A of validation therefore rejects module hooks unless you
validate with `--trust-tools`. Writing hooks is covered in
[extending.md](extending.md#writing-a-hook).

## Sandbox, skills and evals

**`sandbox`** sets limits for `run_code` and Docker module tools. Upper bounds
are ASSUMED sanity caps.

| Key | Default | Bound |
|---|---|---|
| `timeout_s` | 30 | 0 < x <= 600 |
| `output_cap_bytes` | 65536 | 0 < x <= 16 MiB |
| `memory_mb` | 512 | 0 < x <= 16384 |
| `cpus` | 1.0 | 0 < x <= 16 |
| `pids` | 128 | 0 < x <= 4096 |
| `network` | false | |
| `state_mode` | `stateless` | only `stateless` is accepted |

There is no `image` key: the image is an environment choice
(`HARNESS_DOCKER_IMAGE`; [docker.md](docker.md)).

**`skills`** (`enabled`, `root`, `top_k`, `include_user_skills`,
`include_bundled`) configures the skills extension. Skills are a later
milestone and are **not documented here**. `include_bundled: true` is always an
error.

**`evals`** is either `{file: evals/cases.yaml}` or an inline list of cases. The
case format is in [evals.md](evals.md).

## Errors

A bad spec raises `SpecError`, with the file name and a dotted path. The CLI
prints it as `error: ...` and exits 2.

```text
harness.yaml: budget.max_stepz: Extra inputs are not permitted
```

| Problem | Class | Location in the message |
|---|---|---|
| unknown key, wrong type, missing field | `SpecError` | the field path |
| module path escapes the directory or is missing | `SpecError` | `tools[i].module` |
| bad function name, or a module hook function absent | `SpecError` | `tools[i].function` |
| duplicate tool name | `SpecError` | `tools[i]` |
| `sandbox.state_mode: session`, out-of-range limit | `SpecError` | `sandbox.<field>` |
| bad inline eval case, or bad eval file | `SpecError` | `evals[0].check.text` or `cases.yaml: [0].input` |
| `runtime: host` or a module hook without trust | `ConfigError` | `tools[i].runtime` |
| missing or invalid `tools.schema.json` | `ConfigError` | `tools[i]` |
| `.harness-meta.json` status `fail` | `ConfigError` | `.harness-meta.json` |

`.harness-meta.json` is checked only for a directory with that file: no file (a
hand-written harness) is fine, `pass` and `incomplete` are runnable, `fail`,
an unknown status or an unreadable file are refused until you pass
`--allow-failed`.

## Loading a spec from Python

`HarnessBuilder.from_spec` accepts a file, a directory, a parsed `HarnessSpec`
or a mapping (for example a trace's `spec_snapshot`). Code-only settings
(`.llm`, `.tools`, `.approver`) can be chained afterwards.

```python
from harness_builder import HarnessBuilder, Settings
from harness_builder.errors import SpecError

settings = Settings(model="openai/gpt-4o-mini")
try:
    HarnessBuilder.from_spec(
        {"schema_version": 1, "name": "demo", "budget": {"max_stepz": 5}},
        settings=settings,
    )
except SpecError as exc:
    print(exc)  # <spec>: budget.max_stepz: Extra inputs are not permitted

builder = HarnessBuilder.from_spec(
    {"schema_version": 1, "name": "demo", "budget": {"max_steps": 5}},
    settings=settings,
)
```

Relevant keyword arguments: `trust_host_tools`, `allow_failed`, `base_dir` (the
harness directory, required to rebuild a snapshot of a harness that has module
tools, eval files or a skills root) and `flagged_tools`.

## Known gaps

- `harness run` and `harness eval` do not apply the safety screen's
  `flagged_tools`, so a flagged tool in a generated harness gets no `ask` rule at
  run time. Choosing how to enforce this is open (review item).
- `harness replay` has no option for the harness directory, so a trace from a
  harness with module tools, an eval file or a skills root cannot be rebuilt from
  its snapshot by the CLI (`<spec>: tools[0].module: file references need a spec file or
  directory`, exit 2). Use `HarnessBuilder.from_spec(snapshot, base_dir=...)` in Python.
- A sub-agent profile cannot list extension tools (skills) by name.
- Custom code (hooks, approvers, context policies) cannot be named in a spec
  except through module hooks or `register_builtin_tool`/`register_builtin_hook`.
