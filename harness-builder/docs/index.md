# Documentation index

Start with the [README](../README.md) for install, configuration and quickstarts.
Everything here was exercised offline only (scripted fake model, fake or emulated
Docker). Read the [eval report](eval-report.md) for exactly what that means.

## Guides by task

| I want to... | Read |
|---|---|
| Understand the layers and the step loop | [layers.md](layers.md) |
| Write a tool, hook, context policy or strategy | [extending.md](extending.md) |
| Choose between ReAct and CodeAct | [strategies.md](strategies.md) |
| Inspect, replay or diff a run | [traces.md](traces.md) |
| Set up Docker and the sandbox image | [docker.md](docker.md) |
| Know what the sandbox does and does not block | [isolation.md](isolation.md) |
| Describe a harness in YAML | [spec.md](spec.md) |
| Generate, validate and run a harness | [generator.md](generator.md) |
| Write eval cases and run `harness eval` | [evals.md](evals.md) |
| Use skills, approval and hygiene commands | [skills.md](skills.md) |
| See what was measured and what was not | [eval-report.md](eval-report.md) |

## Examples

Runnable, offline, in [`../examples/`](../examples/README.md):
`react_harness.py`, `codeact_harness.py`, `harness.yaml` with `fake_llm.json`.

## Project coordination files

`shared/` holds the plan, designs, reviews and implementation notes
(`shared/implementation-notes.md`, `shared/eval_results.json`).
