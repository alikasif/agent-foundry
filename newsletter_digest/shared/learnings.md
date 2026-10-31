# Learnings

- (T002) Rewriting `shared/task_list.json` with `json.dump(..., ensure_ascii=False)` turns
  the existing `—` escapes into literal em dashes and produces a huge diff. Use the
  default `ensure_ascii=True` (and indent=2) so a status change is a one-line diff.
- (T002) `uv run ruff format .` reformats sibling agents' in-progress files; use
  `uv run ruff format <own files>` and `uv run ruff format --check .` for the project-wide check.
- (T003) Pydantic v2 checks `Field(min_length=...)` during core str validation, BEFORE
  `field_validator` (mode="after") runs, so `Field(min_length=1)` + a `.strip()` after-
  validator still accepts `" "`. Raise `ValueError` for empty text inside the validator.
- (T003) `shared/task_list.json` has no trailing newline; strip it when rewriting so a
  status change stays a one-line diff.
- (T010) pyrefly types `ChatOpenAI.openai_api_key` as `SecretStr | Callable`, so
  `model.openai_api_key.get_secret_value()` fails type checking after an `is not None`
  check. Narrow with `assert isinstance(model.openai_api_key, SecretStr)` first.
- (T010) pytest-cov is not a project dependency; measure coverage without editing
  pyproject via `UV_HTTP_TIMEOUT=120 uv run --with pytest-cov pytest --cov=...` (the first
  fetch timed out at uv's default HTTP timeout).
- (T006) To check that package data ships in the wheel, `uv build --wheel -o <scratch>`,
  extract it with `python -m zipfile -e` and import from it via `PYTHONPATH` using the
  project venv. Do not create a throwaway venv with `uv pip install` (forbidden here).
- (T006) Jinja autoescape escapes `"` inside a `<script>` body, which breaks JS and
  changes the CSP hash; the static script must be emitted with `|safe`. jinja2 3.1 no
  longer exports `Markup`.
- (T010) `git commit -- <paths>` commits the WORKING-TREE content of those paths, not
  the staged hunks. On shared files such as implementation-notes.md that pulls in other
  agents' uncommitted sections (a737e72 swept in the T006 notes). For shared files, stage
  only your own hunks (e.g. `git diff file > p; edit p; git apply --cached p`) and run a
  plain `git commit` with no pathspec.
- (T006) The shared notes files are appended by several agents at once; a
  `git commit <path>` commits the whole working-tree file, so another agent's commit can
  carry your section (T006's notes landed in the T010 commit a737e72). Harmless, but
  check `git show --stat HEAD` before assuming your notes are still uncommitted.
- (T004) When a dedupe rule relaxes thresholds for "same host", exclude click-tracker
  hosts: every link in one newsletter issue can share the tracker host, so it is not
  evidence that two articles share a destination.
- (T013) Quick mutation check for a test file without touching source: a scratch script
  that patches the module (e.g. `render.ENVIRONMENT.autoescape = False`) and then calls
  `pytest.main([...])` run via `uv run python <scratch>/mut.py` shows whether the tests
  actually guard the property. Keep the script in the scratchpad.
- (T013) `shared/task_list.json` usually carries other agents' uncommitted status edits;
  update your task's status in the working tree but do not commit the file unless asked.
- (T005) langchain-mcp-adapters 0.3.2 `get_tools()` spawns a new stdio server per tool
  call; use `client.session(name)` + `load_mcp_tools(session, ...)` for one process.
  A tiny `mcp.server.fastmcp.FastMCP` stdio script run with the venv's python is a
  quick way to exercise the real adapter path end to end without Gmail OAuth.
- (T005) pyrefly warns (deprecated) on `@asynccontextmanager` functions annotated
  `-> AsyncIterator[T]`; annotate `-> AsyncGenerator[T, None]` instead.
- (T005) The Gmail MCP server reports handler failures as normal text `Error: ...`
  (no `isError`), so `handle_tool_errors=False` alone does not catch them.
- (T007) langchain-core 1.7.3 `StructuredTool(handle_validation_error=<callable>)` turns
  argument-schema `ValidationError`s into the callable's string on both `ainvoke(dict)`
  and `ToolCall` paths (ToolMessage status "error"), so a tool can "never raise" on bad
  args without relying on ToolNode error handling.
- (T007) `requires-python >= 3.11`: ruff rejects PEP 695 `def f[T: X](...)` as invalid
  syntax; use `typing.TypeVar`.
- (T012) To test code that calls `langchain_mcp_adapters.tools.load_mcp_tools(session)`
  without patching it, a fake session only needs `async list_tools(cursor=None)`
  returning `mcp.types.ListToolsResult(tools=[mcp.types.Tool(name=..., inputSchema=
  {"type": "object", "properties": {}})])` and `async call_tool(name, arguments, **kw)`
  returning `mcp.types.CallToolResult(content=[TextContent(...)])`. A FastMCP stdio
  script run with `sys.executable` starts in well under a second, so a real-subprocess
  test is cheap. Wrap it in `anyio.fail_after` so a hang fails the test.
- (T012) A minimal `BaseTool` fake for `ainvoke(dict)`: subclass `BaseTool` without
  `args_schema`, implement `async _arun(self, **kwargs)`. The dict is passed through
  as kwargs, so you can record the exact argument names.
