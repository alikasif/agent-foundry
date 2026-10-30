# Implementation Notes

## Library verification

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V0 | Versions pinned by `uv add` for every dependency | T001 | see Result column | `uv run python` + `importlib.metadata.version(...)` after `uv sync` | Pinned in uv.lock (Python 3.13.5 venv): deepagents 0.7.32, langchain-mcp-adapters 0.3.2, langchain-openai 1.7.3, langchain-core 1.7.3, langgraph 1.2.18, mcp 1.30.0, pydantic 2.14.2, jinja2 3.1.6, click 8.5.0, anyio 4.15.1, python-dotenv 1.2.4, rich 15.0.0. Dev: pytest 9.2.0, pytest-mock 3.17.0, ruff 0.17.3, pyrefly 1.4.0, trio 0.34.0. deepagents and langchain-mcp-adapters match shared/verified_facts.md |
| V1 | `create_deep_agent` signature: `system_prompt` (not `instructions`), `model` accepts instance, `name` kwarg | T008 | TODO | TODO | TODO |
| V2 | `SubAgent` keys; per-subagent `model` supported; `tools` accepts tool objects (not names) | T008 | TODO | TODO | TODO |
| V3 | [re-verify] default backend is in-state/in-memory (never disk); pass state backend explicitly if not | T008 | TODO | TODO | TODO |
| V4 | [re-verify] whether the default `general-purpose` subagent can be disabled; if not, prompt forbids it and risk is noted | T008 | TODO | TODO | TODO |
| V5 | [re-verify] which built-in tools (filesystem, todos, task) the main agent and reader see | T008 | TODO | TODO | TODO |
| V6 | `MultiServerMCPClient` constructor and methods; not an async context manager; [re-verify] whether `get_tools()` opens a session per call; exact import path of `load_mcp_tools` and `client.session(name)` | T005 | TODO | TODO | TODO |
| V7 | Gmail MCP server tool names (allowlist `search_emails`/`read_email` present) and argument names (`query`, `maxResults`, `messageId`) | T005 | TODO | TODO | TODO |
| V8 | `search_emails` output format (`ID:/Subject:/From:/Date:` blocks), no pagination, `label:` query operator works | T005 | TODO | TODO | TODO |
| V9 | `read_email` output: headers + text/plain body, else raw HTML with the `[Note: This email is HTML-formatted...]` prefix | T005 | TODO | TODO | TODO |
| V10 | Shape of MCP tool results returned by langchain-mcp-adapters (str vs content-block list vs tuple) | T005 | TODO | TODO | TODO |

## Task notes

### T001 — Scaffold

- **Deviation (pyrefly in dev group):** `pyrefly` added to the dev group because repo
  CLAUDE.md requires `pyrefly check`; the PRD's FR-020 dev list omits it.
- **Deviation (anyio backend):** `tests/conftest.py` pins `anyio_backend` to `"asyncio"`
  only. LangChain, langgraph and the MCP stdio client are asyncio-based. `anyio[trio]` is
  still installed (repo convention) but trio is not used as a test backend.
- **pyrefly config:** `[tool.pyrefly] project-includes = ["src", "tests"]`. `uv run pyrefly
  check` reports 0 errors on the stubs.
- **`uv init` defaults overridden:** it generated `requires-python = ">=3.13"`, a
  `.python-version` (3.13), a script entry `newsletter_digest:main` and a hello-world
  `__init__.py`. Rewrote `requires-python` to `>=3.11`, deleted `.python-version` (the venv
  still resolves to Python 3.13.5 locally), pointed the script at
  `newsletter_digest.cli:main` and replaced the generated code.
- **README.md stub kept:** `pyproject.toml` has `readme = "README.md"` and hatchling fails
  to build if the file is missing, so a 3-line placeholder remains. T018 overwrites it.
- **Dropped `authors`** from pyproject (uv init guessed an email from git config).
- **Version specifiers:** `uv add` wrote lower bounds (`>=` current version) rather than
  exact pins; `uv.lock` (committed) holds the exact resolved versions.
- **pytest exit code:** with no tests collected `uv run pytest` exits 5, which the task
  accepts. It becomes 0 once T010+ add tests.
- **Network:** `uv add` took more than 120s on the first run (cold cache); it succeeded
  without retries once `UV_HTTP_TIMEOUT=120` was set.

### T002 — config.py and llm.py

- **Heading level:** `###` under "Task notes" to match the T001 section (the task text says `##`).
- **ChatOpenAI kwargs (langchain-openai 1.7.3, probed with `uv run python` on
  `ChatOpenAI.model_fields`):** `model` (alias of `model_name`), `base_url` (alias of
  `openai_api_base`), `api_key` (alias of `openai_api_key`, stored as `SecretStr`, absent
  from `repr`), `temperature`, `max_retries`, `timeout` (alias of `request_timeout`).
  `make_model` passes exactly the designs.md kwargs; pyrefly accepts the plain `str` key.
- **Where the D1 logs live:** `build_query` stays pure (no logging). `Settings.build` logs
  at INFO "--label ... is ignored because --query is given" and "--query already has a
  date operator ..." (logger `newsletter_digest.config`). The label log fires only when a
  non-blank label was passed explicitly (CLI flag or `NEWSLETTER_LABEL` env), not for the
  built-in default.
- **Blank = unset:** `None` or whitespace-only `api_key`, `model`, `reader_model`,
  `api_base`, `label`, `query`, `mcp_command`, `mcp_args` all fall back to their default
  (or fail for key/model). Values are `.strip()`ped. A blank `--query` becomes `None`.
- **Normalised stored values (not in spec):** `Settings.window` stores the stripped,
  lower-cased spec (`" 2W "` -> `"2w"`); `log_level` is upper-cased.
- **Extra validation (not in spec):** `log_level` must be one of DEBUG/INFO/WARNING/ERROR/
  CRITICAL (the CLI uses a choice, but `build` is also callable directly); an unparsable
  `GMAIL_MCP_ARGS` (`shlex.split` ValueError, e.g. unbalanced quotes) becomes a
  `ConfigError`. Validation order: key, model, window, max_emails, log level, MCP args.
- **`window_to_days`:** `Nd` accepts leading zeros (`07d` -> 7); `0d`, `366d`, `3w`, `2m`,
  bare numbers and inner spaces (`1 w`) are rejected with a message listing the valid forms.
- **Label quoting:** quoted when it contains any whitespace (`re.search(r"\s", ...)`);
  labels containing `"` are not escaped (user-owned config, Gmail has no escape syntax).
- **`safe_summary()`** returns raw field values (`out_dir` stays a `Path`,
  `gmail_mcp_args` a tuple) for every dataclass field except `openrouter_api_key`; it does
  not include the derived `recursion_limit`.
- **Constants** (`DEFAULT_API_BASE`, `DEFAULT_LABEL`, `DEFAULT_MCP_COMMAND`,
  `DEFAULT_MCP_ARGS`, `MIN_MAX_EMAILS`, `MAX_MAX_EMAILS`, `NAMED_WINDOWS`, `LOG_LEVELS`) are
  module-level so cli.py/tests can reuse them.
- **Verification:** tests are owned by T010; T002 was checked with a scratch script
  (all acceptance criteria incl. frozen dataclass, repr/safe_summary key exclusion,
  recursion limit 100/480/1600, shlex parsing, every ConfigError path, and `make_model`
  attributes). `uv run pytest` still exits 5 (no tests collected yet).
