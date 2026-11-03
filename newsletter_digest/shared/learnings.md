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
- (T005 fix) Exceptions raised inside an `async with` over an mcp `ClientSession` /
  `stdio_client` (anyio task groups) come out wrapped in nested single-member
  `ExceptionGroup`s, so a plain `except SomeError` around it never matches. Fake
  sessions without a task group hide this: test error propagation against a real stdio
  FastMCP server.
- (T007) langchain-core 1.7.3 `StructuredTool(handle_validation_error=<callable>)` turns
  argument-schema `ValidationError`s into the callable's string on both `ainvoke(dict)`
  and `ToolCall` paths (ToolMessage status "error"), so a tool can "never raise" on bad
  args without relying on ToolNode error handling.
- (T007) `requires-python >= 3.11`: ruff rejects PEP 695 `def f[T: X](...)` as invalid
  syntax; use `typing.TypeVar`.
- (T019) Other agents may leave files STAGED in the shared index; a plain `git add <mine>
  && git commit` then commits their work too. Check `git diff --cached --name-only` first;
  for a file only you own, `git commit -m ... -- <path>` commits just that path and leaves
  the others staged (undo a sweep with `git reset --soft HEAD~1`).
- (T019) Pydantic only wraps ValueError/AssertionError raised in validators; an
  AttributeError from a `mode="before"` validator (e.g. `.casefold()` on an int) escapes
  `model_validate` raw, so `except ValidationError` in tool code does not catch it.
- (T007) The git index is shared by all concurrent agents: anything you stage can be
  swept into another agent's plain `git commit` fired at the same moment (T019's commit
  briefly carried T007's staged files before it was reset). Stage and commit in one
  command (`git apply --cached p && git add <own files> && git commit ...`), then check
  `git show --stat HEAD` and `git reflog` before reporting.
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
- (T011) Before writing fuzzy-dedupe tests, measure the candidate titles with
  `titles_match(normalize_title(a), normalize_title(b), same_host=...)` in a one-off
  `uv run python -` snippet. Intuitive "near-duplicate" pairs often fall below 0.92,
  and two stored titles can both match a new one only if they are below 0.92 to each other.
- (T011) Mutation-testing the store lock (swap `self._lock` for
  `contextlib.nullcontext()` in a scratch script) shows whether the threading smoke test
  can catch a race. A Barrier plus single-article `add_articles` calls from 8 threads
  caught it here.
- (T003 fix) A pydantic `mode="before"` validator receives raw input of any type, and
  only `ValueError`/`AssertionError` raised inside it become a `ValidationError`.
  Type its helper's parameter as `object` and handle non-strings explicitly; a stray
  `.casefold()` on an int escapes `model_validate` as a raw `AttributeError`.
- (T014) A fake raw MCP tool for wrapper tests: `StructuredTool.from_function(
  coroutine=<async def f(**kwargs)>, args_schema=<JSON-schema dict>)`. With a dict schema
  langchain-core passes the input dict straight through as kwargs, so the fake does not
  need its own pydantic model and the test can key it by `GMAIL_ARG_MAP["read_id"]`.
- (T008) deepagents 0.7.32 binds `recursion_limit=9_999` via `.with_config`, but a
  per-call `ainvoke(..., config={"recursion_limit": n})` overrides it. The default
  general-purpose subagent can only be turned off with the global, beta
  `register_harness_profile` registry (or by passing your own `general-purpose` spec).
  Built-in file tools can be trimmed per agent by passing
  `FilesystemMiddleware(backend=..., tools=["read_file"])` in `middleware` (it replaces
  the default one by name). `read_file` is mandatory there.
- (T008) To see exactly which tools each deepagents model gets, without an LLM: subclass
  `BaseChatModel` with `bind_tools` that records the tool names and returns `self`, plus
  a scripted `_generate`. The tools node of the built graph is
  `graph.nodes["tools"].bound.tools_by_name`; the `task` tool description lists the
  available subagents.
- (T008) pyrefly rejects `FilesystemMiddleware` inside a `SubAgent["middleware"]` list
  (invariant `list[AgentMiddleware]` vs `FilesystemState`). A targeted
  `# pyrefly: ignore[bad-assignment]` on that key is the least-bad fix.
- (T009) `rich.console.Console(stderr=True)` resolves `sys.stderr` on every print, so a
  module-level console (and a RichHandler bound to it) is captured by click's
  `CliRunner` in `result.stderr` (click 8.5 always separates stderr). Configure logging
  before `Settings.build`, or its INFO notes (e.g. "--label ignored") are dropped.
- (T018) README claims are cheap to get wrong: check each against source before writing
  (e.g. the "mentioned in" badge only renders when `mentioned_in > 1`; mark-read keys are
  `sha1(url or id)[:16]` under a prefix, not per file path; `--query`, `--window`,
  `--max-emails`, `--out`, `--log-level` have no env var). `uv run newsletter-digest
  --help` lists all nine options plus `-h`.
- (T017) This repo's `pyproject.toml` already deselects the `integration` marker via
  `addopts`. An autouse fixture that calls `pytest.skip` runs before the test's other
  fixtures, so it can gate a live test before anything expensive (`Settings.build`, MCP
  start) is built. Import `newsletter_digest.cli` inside the test: it runs
  `load_dotenv()` at import time.
- (T016) python-dotenv's bare `load_dotenv()` searches upward from the calling module's
  file, not the cwd, unless it is in a REPL, `python -c`, or a trace function is active.
  So it behaves differently under `pytest --cov` (cwd) than in the console script
  (package dir). A subprocess test of `.env` discovery must drop `COV_CORE_*` /
  `COVERAGE_*` from the child env, and it must run a script file, not `python -c`.
- (T016) Rich stderr output under `CliRunner` wraps at 80 columns, and long tokens are
  folded mid-word. Collapse whitespace (`" ".join(s.split())`) before substring checks,
  and remove all whitespace before a "secret not in output" check.
- (T015) Scripted fake chat models for deepagents must answer as a pure function of the
  messages they get: every parallel `newsletter-reader` task shares ONE reader model
  instance, so a counter or queue of replies interleaves between tasks. Take the email id
  from the task's human message and the step from the ToolMessages present.
- (T015) Dropping the per-call `recursion_limit` does not make a looping real deepagents
  graph fail; it runs to the bound 9_999 (minutes). Wrap real-graph tests in
  `anyio.fail_after(...)`, and assert `len(model.replies) <= settings.recursion_limit`.
- (T015) Mutation-test a module without editing it: in a scratch script, read the source,
  `str.replace` one line, `exec` it into a `types.ModuleType("newsletter_digest.agent")`
  registered in `sys.modules` (and as the package attribute) and call `pytest.main`.
  Tests that do `import newsletter_digest.agent as m` then see the mutant.
- (T009 fix) On this machine `env` on PATH is uv's `~/.local/bin/env`, a shell snippet
  meant to be sourced. Run as a command it ignores its args and exits 0 with no output,
  so `env -u VAR cmd` silently runs nothing. Use `/usr/bin/env -u VAR cmd`.
- (T007 fix) Wrapping a `StructuredTool` coroutine in `async def guarded(**kwargs)`
  is safe when `args_schema` is explicit: langchain-core only passes the keys the
  caller supplied, so the inner coroutine's defaults still apply, and with `**kwargs`
  it injects no `callbacks`/`config`. `traceback.format_tb(exc.__traceback__)` gives a
  traceback without the exception message, which helps when that message may hold
  email text.
- (T020 round 2) To get the caller's exception back out of an `@asynccontextmanager` that
  wraps mcp/anyio task groups: record the exception at the `yield`, catch
  `BaseExceptionGroup` around the stack, and re-raise the recorded object only if it is
  the identical sole leaf. Do the re-raise outside the `except` block so the group does
  not become `__context__`. contextlib then sees the same object and returns False, so
  the caller keeps its own traceback. Probe timeout, `task.cancel()` and
  `KeyboardInterrupt` too: they must come out as themselves.
- (T020 round 2) `load_dotenv(find_dotenv(usecwd=True))` with no `.env` is a no-op:
  `find_dotenv` returns `""`, and python-dotenv 1.2.x only falls back to its own search
  when `dotenv_path is None`.
- (T020 round 2) `shared/task_list.json` is written with `json.dumps(indent=2,
  ensure_ascii=True)` and no trailing newline. Match that exactly, or a status edit
  rewrites every line containing non-ASCII text.
- (T022) `Settings.build` stops at the first ConfigError, checking the API key, then the
  model, then the window. A smoke test of `--window 3x` with no key therefore prints the
  missing-key error, which still exits 2. To check the window message, pass a dummy key
  and `--model x/y`. To run the console script from an empty cwd, so that no `.env`
  is found, use `uv run --project <project_dir> newsletter-digest ...`.
- (T023) pyrefly's import root here is `src` (`[tool.pyrefly]` has only
  `project-includes`), so `from tests.<helper> import ...` in a test file is a
  `missing-import` error, even though pytest resolves it (`tests` is a package). A
  relative `from .helper import` fails the same way. Either add `search-path = ["."]`
  to `[tool.pyrefly]` or use a targeted `# pyrefly: ignore[missing-import]`.
- (T023) Run `uv run ruff check .` before rendering goldens from a fixture module. A
  line-length fix after rendering means rendering again (or at least checking the sums
  again).
- (T024) `search-path = ["."]` in `[tool.pyrefly]` makes `from tests.x import ...` resolve;
  with it the `# pyrefly: ignore[missing-import]` comments are unneeded. `uv add` of
  already-transitive deps (httpx, httpcore) just promotes them to direct dependencies.
- (T024) `uv run ruff format --check .` also checks code blocks in `shared/*.md`; use
  `ruff format <own files>` and `ruff check .` instead of a whole-tree format check.
- (T027) Parallel agents dispatched from one session share the SAME scratchpad
  directory: a generic `probe.py` written there was overwritten by another agent between
  the write and the run (the output came from their extract probe). Prefix scratch
  files with your task id (`t027_probe.py`).
- (T025) When keyword-matching URLs, `urlsplit` BEFORE `unquote`: decoding first turns
  `%23`/`%3F` into `#`/`?`, so `/a%23unsubscribe` moves the keyword into the fragment
  and a substring check over path+query misses it.
- (T029) trafilatura 2.3.1 `extract(..., max_tree_size=N)` raises `ValueError`
  ("use settings.cfg instead") for any truthy N, even though the kwarg is still in the
  signature. Pass `config=` with `MAX_TREE_SIZE` set on a copy of
  `trafilatura.settings.DEFAULT_CONFIG`. Check deprecation shims in the source, not just
  `inspect.signature`.
- (T029) In HTML fixtures, a raw `<<<END ARTICLE>>>` is parsed by lxml as tags and
  disappears before extraction, so a delimiter-scrub test built with it passes
  trivially. Use `&lt;&lt;&lt;END ARTICLE&gt;&gt;&gt;` to get the look-alike into the
  extracted text.
- (T028) pyrefly types `httpcore.AnyIOBackend()` as a union with httpcore's fallback
  stub class (defined when anyio is missing, not an `AsyncNetworkBackend`), so passing it
  where an `AsyncNetworkBackend` is expected fails. Assign it to an annotated local with
  `# pyrefly: ignore[bad-assignment]` on the line above.
- (T028) `httpx.URL` keeps inet_aton-style hosts (`2130706433`, `0x7f.1`, `127.1`)
  unchanged and only rejects some (`0177.0.0.1`), so URL-level numeric-host checks must
  be our own. It also turns an explicit default port into `port=None`. Netguard's
  `BlockedAddressError` and a raw `socket.gaierror` from the resolver both reach
  `client.get` callers unmapped as `OSError`s; check the specific class first.
- (T033) A "no `src=` in the page" assertion fails on hostile fixtures that contain the
  text `<img src=x ...>`: autoescape renders `&lt;img src=x`, so the substring is still
  there. Match attributes inside real tags (`re.search(r"<[^>]*\ssrc=", html)`).
- (T033) `tests.flag_off_fixture` is importable from a scratch script only with the
  project dir on the path: `PYTHONPATH=. uv run python <scratch>.py`.
- (T035) A pydantic `mode="before"` validator that only handles `isinstance(value, list)`
  is bypassed by tuples and sets: lax mode coerces them into `list[str]` after the
  validator returned them unchanged. Test sequence validators with a tuple too.
- (T034) pytest-cov is not installed here (`--cov` is "unrecognized arguments"). During a
  parallel batch, measure one module's line coverage without touching `pyproject.toml`:
  `sys.settrace` before the first import, run `pytest.main`, and compare the hit lines
  with the `co_lines()` of the module's compiled code objects, walked recursively.
- (T036) pytest-cov is not installed here. `uv run --with coverage coverage run --branch
  --source=src/newsletter_digest -m pytest <file>` measures coverage (branches too)
  without editing `pyproject.toml`/`uv.lock`. Set `COVERAGE_FILE` to a scratch path.
- (T036) `str(IPv6Network("::ffff:0:0/96"))` renders as `::ffff:0.0.0.0/96`, so compare
  network lists as parsed `ip_network` objects, not strings. `network_address + 1` on a
  `/32` or `/128` raises `AddressValueError`; guard it with `num_addresses > 1`.
- (T037) lxml drops raw C0 control bytes in HTML before trafilatura returns, so a
  fixture page cannot prove that our own control-character strip works. Test
  post-processing by monkeypatching `trafilatura.extract` to return exact raw text.
  Numeric entities for C0 controls (`&#7;`) make trafilatura 2.3.1 return None for the
  whole page.
- (T037) Check a `Callable[...]` alias against its default implementation with pyrefly
  in a scratch file (`x: Alias = func`). AF-P5's positional `Extractor` alias does not
  accept the keyword-only `extract_article_text`.
- (T030) httpx 0.28.1 with `follow_redirects=True` calls `response.aread()` on every
  redirect response, which reads and decodes (gzip/br/zstd) the body with no size bound.
  A 302 carrying a 49 KB gzip of 50 MB costs 100 MB of memory. Drop redirect bodies in a
  `response` event hook: close `response.stream` (NOT `response.aclose()`, which makes the
  later `aread()` raise `StreamClosed`) and assign `httpx.ByteStream(b"")`.
- (T030) `httpx.Response(..., content=b"...")` (what MockTransport handlers return) is
  read at construction time, so `aiter_raw()` on it raises `StreamConsumed`, and an
  encoded body is decoded inside the handler. Iterate `response.stream` for raw bytes,
  and use an async generator as `content=` when testing encoded bodies.
- (T030) In httpx 0.28.1 env proxies are mounted only when `trust_env and transport is
  None`, and there is no `.netrc` support. With a custom transport, `trust_env=False` is
  defence in depth, not the thing that blocks `HTTP(S)_PROXY`.
- (T040) Mutation-test a Jinja template without touching the source: in a subprocess,
  set `render.ENVIRONMENT.loader = DictLoader({render.TEMPLATE_NAME: mutated_src})`
  before `pytest.main([...])`. Hostile text that renders escaped still contains `url(`
  as plain text, like `src=` (T033): match it inside real tags or within `<style>` only.
- (T031) openai 3.x ships its own `httpx2` package; `ChatOpenAI(http_async_client=...)`
  needs an `httpx2.AsyncClient`. `httpx2.MockTransport` gives an offline probe of the real
  ChatOpenAI error path (e.g. a 429 after `max_retries`) without any network.
- (T031) A pacer with only an injectable clock still sleeps for real; inject the sleep
  function too, or a fake-clock test waits the full interval.
- (T038) httpx parses a redirect `Location` before the request hook runs. An
  unparseable one (`http://0177.0.0.1/`) becomes `RemoteProtocolError` ("Invalid URL in
  location header"), so a policy test on that hop sees NETWORK, not the hook's UNSAFE.
  Use hosts httpx accepts (`0x7f.0.0.1`, `2130706433`) to reach the URL policy.
- (T038) To prove "rejected unread", pass an async generator body that counts its
  yields (and can repeat forever with an `anyio.sleep(0)` per chunk) as `content=`;
  assert zero yields. An endless body also makes a missing cap show up as a timeout
  instead of a silent pass. For a bomb, also bound the `tracemalloc` peak: an unbounded
  decoder still ends in TOO_LARGE.
- (T032) trafilatura attaches a `NullHandler` to its package logger, so its per-page
  WARNINGs ("discarding data: None") never reach stderr when only the project logger has a
  handler (no last-resort output). Probe a third-party logger's handlers before adding
  code to quiet it.
- (T032) click 8.5 `is_flag` + `envvar`: blank/whitespace -> False, an unknown value
  (e.g. `2`) -> exit 2 naming the option and env var; the CLI flag beats an env `0`.
- (T039) In an anyio task-group pipeline where every task is started at once, a
  "breaker open?" check placed before `async with limiter` runs for all tasks at start-up,
  so it never fires for tasks that are merely queued on the limiter. Check shared state
  after acquiring the limiter. A strict-xfail test with slow fetches and a fast 429 shows it.
- (T039) pyrefly cannot pick a `sorted(..., key=f)` overload when `f` takes
  `list[BaseMessage]` and the iterable is inferred as `list[HumanMessage | SystemMessage]`
  (list invariance). Annotate the container as `list[BaseMessage]`.
- (T041) `logger.info("Settings: %s", some_dict)` stores the dict itself in
  `record.args` (logging unwraps a lone mapping argument), not a 1-tuple. Read it with
  `isinstance(record.args, dict)` when a test needs the logged object.
- (T042) An AST "only X references Y" rule must skip docstrings and comments: enrich.py's
  docstring names `ChatOpenAI` and extract.py's names `trafilatura.fetch_url`. Match
  names, attributes and import aliases, plus string constants only on exact equality.
- (T042) To mutation-test an AST guard without touching `src/`, read the source dir from a
  module global in a module-scoped fixture and let a scratch `-p` plugin overwrite it in
  `pytest_collection_modifyitems` (`item.module.SOURCE_DIR = Path(copy)`).
- (T044 fix) Per-call state for an httpx request hook (e.g. the hops of one redirect
  chain): pass a mutable object in `client.stream(..., extensions={key: obj})`. httpx
  0.28 builds every redirect request with `extensions=request.extensions`, and the
  timeout merge copies the dict but not its values, so the hook sees the same object on
  every hop. httpcore ignores unknown extension keys.
- (T044 fix) Never match bot-challenge markers (`captcha`) against raw HTML before
  extraction: `g-recaptcha` / `recaptcha/api.js` on ordinary signup forms match. Decide
  the challenge only when extraction found too little text.
- (T045 fix) A pacer sleep is an await inside a held slot: re-check shared "stop" state
  (a breaker) after every await that can take long, not only after acquiring the slot.
  Test it deterministically by injecting a pacer `sleep` that waits on an `anyio.Event`
  set when the failure is raised (no checkpoint between the raise and the breaker flag).
- (T044 test follow-up) Scope a mutant to one module: replace that module's `httpx`
  global with a `types.ModuleType` proxy (copy `vars(httpx)`, override one function).
  Patching `httpx.create_ssl_context` globally also "kills" tests that call it directly,
  which overstates what the new test catches.
- (T044 test follow-up) To prove `abandon_on_cancel`, let the extractor thread call
  `anyio.from_thread.run_sync(started.set)` and then block on a `threading.Event` that
  is set only at fixture teardown. Cancel once `started` fires and time the return. If
  the source drops the flag, the cancel waits for the thread's bounded wait (3 s).
- (T048) Wheel packaging of non-Python package data needs no hatch change when
  `packages = ["src/newsletter_digest"]` is set: `fonts/*.ttf` and `LICENSE` land in the
  wheel (checked with `uv build --wheel` + `python3 -I -m zipfile -l`).
- (T048) Download untrusted archives into a fresh scratch dir, check the zip sha256
  before extracting, extract with `python3 -I -m zipfile -e`, then check each member.
- (T050) Before "appending" dataclass fields, grep the tests for `dataclasses.fields(...)`
  order assertions: `test_config_fetch.py` pins the fetch fields as the last `Settings`
  fields, so later additions have to go before `reader_parallelism`.
- (T050) When an `__reduce__` raises `TypeError`, `copy.copy`/`copy.deepcopy` of the object
  (and of any dataclass holding it) raise too. Use `dataclasses.replace`, which does not copy.
- (T052) pyrefly bundles an fpdf2 stub older than 2.8.9: it types `set_font(size)` as
  int, colours as int (no hex strings), `multi_cell(link)` as `int | str`, and
  `str_alias_nb_pages` as str. Use RGB int tuples, `set_font_size(float)`, `link=x or ""`
  and one `# pyrefly: ignore[bad-assignment]` for the alias; runtime accepts all of them.
- (T052) fpdf2 2.8.9 font keys in `pdf.fonts` are the lowercased family plus the style
  (`"dejavu"`, `"dejavuB"`). The missing-glyph WARNING is logged on `fpdf.output` during
  `output()`, and fontTools logs dozens of DEBUG table reads per build (noise at root
  DEBUG level).
- (T049) A "module-level import" AST rule must treat `if TYPE_CHECKING:`, `try:` and class
  bodies as module level: walk with `ast.iter_child_nodes`, skip only FunctionDef,
  AsyncFunctionDef and Lambda, then reuse the existing import collector on
  `ast.Module(body=<found imports>, type_ignores=[])`.
- (T049) Stub-first guard rules: keep a synthetic "final shape" tree built from the API
  contract next to the stub tree, and assert every new rule allows both. It catches rules
  that would reject the planned code before any coder writes it, and surfaced a
  TP-5/TD-2 conflict (cli's lazy `redact` import vs `telegram` importable only by
  `delivery`).
- (T055) A `SecretToken` whose `__reduce__` raises also blocks `copy.deepcopy` and
  `dataclasses.asdict` of any dataclass holding it (`copy.copy` and `replace` still
  work). Do not `asdict`/deepcopy a flag-on `Settings`.
- (T058) pypdf `extract_text` on the fpdf2 reading-list PDF puts the footer page number as
  the last line of each page; drop it before joining pages or it lands mid-sentence when
  an item spans a page break. The outline (`reader.outline`) gives the section headings
  directly. `pdf_text` strips leading spaces, so join separators before calling it.
- (T051) httpx 0.28 does not percent-encode `:` in the path, so the `httpx` INFO line holds
  the raw `bot<id>:<secret>`; a `bot\d+:[A-Za-z0-9_-]+` regex matches it. To keep one
  `.reveal()` site and still register the token for redaction, register it in the same
  frame that builds the URL.
- (T051) Budget checks under a fake `sleep`: `scope.deadline - anyio.current_time()` of a
  `move_on_after` scope still measures real loop time, so "delay exceeds the remaining
  budget" is testable with a small `total_deadline` (e.g. 3 s with 2 s/4 s backoff stops
  after the 2 s wait) and zero real waiting.
- (T053) fpdf2 2.8.9 writes page objects uncompressed, so
  `len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))` equals pypdf's page count on the
  fixtures; good enough for a log line without parsing the PDF.
- (T053) For an atomic `.part` + `os.replace` write, unlink the `.part` in `finally` with
  `missing_ok=True` (after a successful replace it is already gone) and suppress OSError
  there, so cleanup can never mask the original error.
- (T057) A monkeypatch mutant applied in a function-scoped autouse fixture does not reach
  module-scoped fixtures (they are built first): patch in the plugin's `pytest_configure`
  instead, or the mutant falsely "survives" (the no-`set_lang` mutant did until then).
- (T057) A mutant that rebuilds a big constant per call (`lambda: frozenset(range(0x110000))`
  for a per-string coverage lookup) makes the run hang; build it once outside the lambda.
- (T057) `from __future__ import annotations` binds the name `annotations` in the module:
  a helper named `annotations()` passes at runtime but pyrefly reports `_Feature` not callable.
- (T054) Rich `Console.print` wraps at the console width, which is 80 columns when stderr
  is not a terminal (CliRunner, pipes, CI). For a message that must stay one line (two
  full paths), pass `soft_wrap=True`.
- (T054) A lazy import that is also the route to a helper (`redact` via `delivery`) needs
  its own `try`: if the import fails, the helper is missing too, so the `except` must not
  use it. Keep the import `try` separate from the call `try`.
- (T056) To mutation-test a module whose names tests import at collection time, a scratch
  `-p` plugin can `exec(compile(mutated_source, mod.__file__, "exec"), mod.__dict__)` at
  plugin import: it runs before the test module's `from mod import ...`, so every class
  and function (including exception classes) is the mutated one and identity checks hold.
  No source edit, safe with parallel agents.
- (T056) `telegram._SECRETS` persists across tests (every token ever sent stays
  registered); swap it for an empty set per test with `monkeypatch.setattr` and restore
  the six redacted loggers' filters afterwards, or redaction tests become order-dependent.
- (T064) README tables are checked by tests only via .env.example; docs claims (retry counts, 50 MB, messages) come straight from telegram.py/delivery.py constants, so re-check them if those change.
