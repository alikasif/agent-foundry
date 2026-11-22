# Examples

All three run offline: no API key, no network, no Docker. Run them from the
harness-builder directory.

| File | What it shows |
|---|---|
| `react_harness.py` | Code-built ReAct harness, one `@tool`, scripted `FakeLLMClient`. |
| `codeact_harness.py` | Code-built CodeAct harness with `run_code` and a `FakeExecutor` (canned result, not a sandbox). |
| `harness.yaml` + `fake_llm.json` | A sample HarnessSpec run through the CLI with the hidden, test-only `--fake-llm` option. |

```text
uv run python examples/react_harness.py
uv run python examples/codeact_harness.py
uv run harness run examples/harness.yaml "Say hello" --fake-llm examples/fake_llm.json --trace demo-trace.json
uv run harness replay demo-trace.json
uv run harness trace show demo-trace.json
```

Expected output of the first two: `answer: 2 + 3 = 5` and `answer: 10`, each with
`stop: done`. For a real model, drop the scripted client and set
`OPENROUTER_API_KEY` (and `HARNESS_MODEL`). For real CodeAct, drop the
`FakeExecutor` and have Docker and the sandbox image ready (`uv run harness doctor`).
