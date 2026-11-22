# Eval report

Source: `shared/eval_results.json`, written by `scripts/run_eval_results.py`
(`uv run python scripts/run_eval_results.py`) on Windows 11 (`Windows-11-10.0.26300-SP0`).

## Read this first

**The fixture-harness evals and the skills comparison below are offline and
Docker-emulated, and every model answer in them comes from a scripted fake LLM. The
passes are by construction.** They show that the plumbing works. They say nothing
about whether a real model or a generated harness is any good.

What ran in a real container (2026-11-22, Docker Desktop server 29.5.3, Windows 11):
`pytest -m docker`, **42 passed, 1 skipped (the live stage-C test), 0 failed**. It
covers the sandbox isolation, resource limits, stdin round trip, harness runs and
latency (`tests/docker`, 31 tests), stage C Docker, `ContainerTool`, the strategy
swap and the skill-script path against a real container, and the `/skill`
read-only mount. This measures the sandbox, not model quality. The first real run
(38 of 42 failed) found one image bug, now fixed: `COPY --chmod=0644` also gave
`/opt/hb` mode 0644, so uid 10001 could not open `hb_entry.py`. Recorded in
`shared/eval_results.json` under `real_docker_tests` (produced by
`scripts/run_eval_results.py --real-docker`). Median container round trip
(T032 latency test, 10 runs): about 1.05 s on this host, target 3.0 s.

Still not measured, anywhere in this project:

- **The fixture harness evals in real Docker.** The numbers below use the
  production sandbox code path with `hb_entry` called in-process
  (`tests/generator/support.py`), not a container. Only the docker-marked tests
  above ran in a container.
- **Live LLM runs.** No request has been sent to OpenRouter. `HARNESS_LIVE` was
  not `1`, so live TaskCases are recorded as a skip, not as a failure.
- **Rubric checks.** They need a judge model, so they were never run.
- **The OQ-15 criterion** (at least 20% fewer steps with skills, no success drop).
  `oq15_measured_live` is `false`. The 33% below comes from a scripted model that
  was engineered to need two extra calls without the skill.
- **Cost.** `cost` is `null` or `0.0` (fake model, no price). No cost figure here is
  real.
- **Generated-harness `py_assert` checks, `run_code` and Docker-runtime ToolCases
  from the fixture evals** inside a real container (the docker-marked tests cover
  these paths with their own fixtures, not the three eval domains).

## What was run

| Item | Value |
|---|---|
| Mode | offline |
| LLM | `FakeLLMClient`, scripted |
| Docker | emulated in-process; `FakeExecutor` for the skills comparison (real Docker: see the docker-marked results above) |
| ToolCases | deterministic (same input, same output) |
| Live TaskCases | skipped, `HARNESS_LIVE is not 1` |

Each fixture harness (`tests/generator/fixtures`) is first written by the pipeline
with a scripted generator and validated (stages A, B0, B, C) with the emulated
sandbox. All three reached `generation_status: pass`. Then `EvalRunner` ran their
cases.

## Fixture harness evals

| Domain | Cases | Passed | Failed | Incomplete | Mean steps | Tokens |
|---|---|---|---|---|---|---|
| `csv_triage` | 4 | 4 | 0 | 0 | 2.0 | 321 |
| `repo_qa` | 4 | 4 | 0 | 0 | 2.0 | 277 |
| `unit_conversion` | 5 | 5 | 0 | 0 | 2.0 | 276 |

Each domain has one TaskCase and the rest are ToolCases (3, 3 and 4). ToolCases
call the generated tool with fixed arguments and compare the output, so they are
real checks of the tool code, but they ran through the emulated sandbox only. The
TaskCase answer is scripted (one example tool call, then the expected text), so it
passes by construction. Tokens are counted by the fake client's token counter. All
tokens come from the TaskCase, and the ToolCases report 0.

Case ids: `csv_triage`: `groups-by-severity`, `counts-by-status`, `empty-export`,
`worst-first` (task). `repo_qa`: `lists-files`, `finds-port`, `reads-changelog`,
`which-port` (task). `unit_conversion`: `km-to-m`, `mile-to-km`, `freezing-in-f`,
`boiling-in-k`, `marathon` (task).

## Skills comparison (FR-057)

`compare_skills` ran the same case twice, with an empty skills library and with one
seeded skill.

| Run | Success rate | Mean steps | Tokens |
|---|---|---|---|
| empty | 1.0 | 3.0 | 478 |
| seeded | 1.0 | 2.0 | 628 |

Step reduction: 0.333. Success not dropped. The scripted model is written so the
empty run takes two extra tool calls. So the 33% is arithmetic on a script. It
proves the comparison mechanics (two runs, step reduction, success check against
the 0.2 threshold, `oq15_mechanics_ok: true`) and nothing else. Seeded tokens are
higher than empty tokens in this run, which the criterion does not look at.

`harness eval --skills-compare` itself needs `--live` per its help text. This
result came from calling `compare_skills` directly with a scripted model.

## Test-suite state

Full `uv run pytest -q` on the same host after the F1 close-out, with Docker
running: 2498 passed, 10 skipped, 0 failed (5 min 19 s; Docker Desktop had auto-updated to engine 29.9.3 by then, so the docker-marked tests passed on both 29.5.3 and 29.9.3). `ruff format --check`, `ruff check` and `pyrefly check` clean.

Remaining skips, each with a reason:

- `HARNESS_LIVE` unset: the live tests, including the live stage-C test.
- Windows: 6 symlink tests (no privilege, WinError 1314) and 1 POSIX-socket-path
  test.

No test skips for lack of a Docker daemon any more.

## Known limits and residual risks

These come from the architecture reviews (T024, T036, T048, T058) and the T059 and
T060 notes.

- **Real-Docker status (2026-11-22).** B0 schema extraction, `ContainerTool` calls,
  stage C, `run_skill_script`, the skill candidate test runner and the read-only
  mount now pass in a real container (see the top of this report). Windows bind
  mounts on Docker Desktop, checked by hand: file sharing works; a read-only mount
  rejects writes with EROFS; a host directory junction inside a mount appears in the
  container as a dangling symlink and is not followed (no host escape); and the
  mount is **case-insensitive** (`/tools/B.txt` resolves to a host `b.txt`), unlike
  a Linux filesystem. Tools should sort by `p.as_posix()` and not depend on case.
  A POSIX host (uid mapping, `fcntl` lock) is still unverified.
- **Sandbox image tag.** The tag is a content hash of `IMAGE_FILES`, so any edit to
  the Dockerfile or `hb_entry.py` changes it. Run `uv run harness doctor` and the
  printed `docker build` command before the docker-marked tests.
- **The safety screen is a heuristic.** `safety/screen.py` is a static AST check.
  Indirection through `getattr`/`setattr`, assigning a builtin to a variable (`e = exec; e(s)`, `bp = breakpoint`), `vars(builtins)['exec']`, `pydoc.locate`,
  data flow and other deserialisers stay out of reach. A finding in any `.py` file
  of the mounted harness tree (or an unparsable or unscreenable file such as
  `.pth`, `.pyc`, `.so`) flags every module tool, which then gets an `ask` rule.
  That is advisory hardening. The container is the boundary. Checked in a real
  container on 2026-11-22: payloads that screened clean or were flagged all ran under
  the production flags with no network (connect fails) and EROFS on `/tools` and `/`.
  The bypass `bp = breakpoint; bp()` still screens clean and is contained the same way. See [generator.md](generator.md) for the screen categories.
- **Isolation is best effort.** A container on a shared kernel is not a hard
  boundary. See [isolation.md](isolation.md).
- **Generator quality unknown.** No live model has written a harness. The repair
  loop's usefulness depends on the model reading the validation report.
- **Failed-harness gate.** `.harness-meta.json` is written at generation time.
  `harness validate` updates it, but never upgrades `fail` to `pass` on a
  `--trust-tools` run, nor on a run with `not_run` cases when the failing run ran
  them all (`all_cases_ran` in the meta; unknown values fail closed). A
  harness with a TaskCase failed under `new --live` can only be cleared by
  `validate --live` on Docker, or run with `--allow-failed`. A harness edited after
  validation is not re-gated.
- **Skills mount gap.** A spec `skills.root` inside the harness directory is refused
  when the spec also has a `runtime: docker` tool. A root given by `--skills-root`
  or `HARNESS_SKILLS_ROOT` that points inside such a directory is **not** checked.
  Live (hand-placed) skills are trusted and unscreened.
- **POSIX stats lock never run.** The `fcntl` branch of the stats file lock was
  written from documentation on a Windows host.
- **Deferred minors** (accepted, listed in the T059 notes): the `Executor`
  protocol has no config so `getattr(executor, "config")` is used (T056 m4); replay
  increments skill stats and writes `source.json` (m7, m14); prune ages from the
  `SKILL.md` mtime (m9); refactors T058 m-A1 to m-A7 (for example the
  `retriever_factory` seam and splitting `cli/skills.py`); T048 mn1 (move screening
  policy out of `spec/loader.py`) and mn2 (record `generator/validate.py` as a
  composition root in designs.md); and the T046 minors m1, m2, m4, m5, m6.
- **CodeAct untagged fences.** An untagged code fence is treated as a final answer
  (pinned by a test). A real model may trigger a false `done`; not measured.
- **`/work/context.txt`** is named in the CodeAct prompt but nothing writes it.

## ASSUMED defaults still pending confirmation

These are design placeholders baked in at the planning defaults. None has been
confirmed by the user, and none was measured.

| ID | Default |
|---|---|
| U1 | CodeAct state is stateless per block |
| U2 | codeact_agent reuse is a lazy import of three helpers (narrows FR-061) |
| U3 | SkillManager reuse through an editable install and a `ScopedSkillManager` subclass |
| U4 | Skills root order: `SkillsConfig.root`, CLI, `HARNESS_SKILLS_ROOT`, nearest `pyproject.toml` |
| U5 | Generated tools run in Docker; host only with `--trust-tools` |
| U6 | Stage C runs deterministic ToolCases in Docker; TaskCases only with `--live` |
| U7 | Generated harnesses go to `./harnesses/<name>/` (committing them is undecided) |
| U8 | Container lifecycle is create / start / inspect / rm (3 s start target unmeasured) |
| U9 | A failing `pre_tool` hook blocks; `post_tool` and `on_stop` failures are ignored |
| U10 | Unknown price plus `max_cost_usd` is a build-time `ConfigError` |
| U11 | CodeAct action space is code blocks plus native tool calls |
| U12 | Traces go to `./.harness/traces/<run_id>.json`, no retention |
| D4, D6, D7, D9-D11 | BM25 retrieval, skill storage layout, Windows 11 + Docker Desktop target, HarnessSpec v1, directory artifacts, validation pipeline |
| OQ-9, 10, 12, 18, 22, 24, 28 | Promotion criteria, auto-approve marker, dedup threshold (0.7 as built, not 0.8), frontmatter limits, deny-unlisted permissions, sub-agent depth 2, naming |
| OQ-13 | 10 steps, 30 s timeout, 64 KiB output, 512 MiB, 1 CPU, 128 pids, 3 repair attempts |
| OQ-14 | Retrieval top-3 hit rate at least 90%, search under 100 ms (checked offline on a fixture only) |
| OQ-15 | At least 20% fewer steps, no success drop (**not measured live**) |

## To close the gaps

1. Docker items are closed (2026-11-22). To refresh them: `uv run harness doctor`, the
   printed `docker build` command, then
   `uv run python scripts/run_eval_results.py --real-docker`.
2. With `OPENROUTER_API_KEY` and `HARNESS_MODEL` set, `HARNESS_LIVE=1 uv run pytest`
   and `uv run harness eval <harness> --live`.
3. For OQ-15, run `uv run harness eval <harness> --skills-compare --live` with a
   real model and a library of real skills. Judge it only after multiple runs.
