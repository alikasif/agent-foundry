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
- (T059) Log records from an async fixture (setup phase) are not in `caplog.records`
  during the test body (call phase). Copy `list(caplog.records)` at the end of the
  fixture, or use `caplog.get_records("setup")`.
- (T059) `deliver_reading_list` catches `Exception`, so an `AssertionError` from a test
  guard (for example the `_default_transport` stand-in) becomes a failed
  `DeliveryOutcome`, not a test failure. Assert on the outcome.
- (T059) Mutating a function the test module imported by name (`from delivery import
  build_caption`) also needs `monkeypatch.setattr(request.module, ...)`, otherwise the
  direct unit tests still call the original and the mutant looks like it survived.
- (T062) To keep an edit to an existing test file additive (diff with only `+` lines),
  do not add names to an existing `from x import ...` line, and do not add a second import
  from the same module, because ruff isort merges it into the existing line. Import the
  parent package instead (`from newsletter_digest import config`, then
  `config.TELEGRAM_ENV_VARS`), and in fixtures use `request.addfinalizer` rather than a
  yield fixture that needs a new `Iterator` import.
- (T062) A subprocess "module not imported" test is only meaningful with a positive
  control. After the CLI run, the child script imports the module itself and asserts that
  the watched names now appear in `sys.modules` (prefix match catches `PIL.Image`). It
  also reports `newsletter_digest.__file__`, so the parent can check that the child
  imported this tree and not a stray copy.
- (T060) click 8.5: a flag option's `.default` is `Sentinel.UNSET`, not `False`; check the
  resolved value with `cmd.make_context(name, []).params[...]`. Click's help wrapper can
  break a hyphenated word (`end- to-end`), so compare help text with all whitespace removed.
- (T060) To prove the CLI never reads an env var, swap `cli.os` for a `types.ModuleType`
  proxy whose `environ` is a recording dict copy and whose `__getattr__` delegates to `os`;
  it is scoped to cli and leaves the real `os.environ` alone.
- (T063) Real-tree mutation proof can live in the suite: read the real source text,
  apply an anchored `str.replace(anchor, new, 1)` in memory (assert the anchor still
  exists), re-parse only that module into a copy of the parsed package and assert the
  checker reports. Use line-free expected substrings; line numbers shift with every edit.
- (T063) Positive architecture detectors should follow call paths, not exact function
  names: cli's lazy delivery import sits in `_run_delivery`, called by
  `_deliver_to_telegram`, not in `_deliver_to_telegram` as the task text says.
- (T061) To inspect the real `TelegramError` in a CLI-level test, subclass
  `TelegramClient` in the `client_factory` and record what `send_document` raises
  before re-raising. Do not count the test's own injected exceptions as leak evidence:
  they hold the token by construction.
- (T061) A non-httpx exception raised inside an `httpx.MockTransport` handler propagates
  unchanged out of `AsyncClient.post`. That makes it an easy way to reach a "truly
  unexpected" catch-all after the token was registered.
- (T061) When exec-mutating `cli.py` in a scratch plugin, replace its import-time
  `load_dotenv(...)` call, or the mutant re-reads the developer's `.env`.
- (T067) On this WSL machine `$PATH` contains Windows directories with spaces
  (`/mnt/c/Program Files/...`). `/usr/bin/env -u X PATH=$S/bin:$PATH cmd` therefore
  splits the value and fails with exit 127. Quote it: `"PATH=$S/bin:$PATH"`.
- (T067) The CLI names the output `reading-list-<reading_list end date>.html/.pdf`, not
  today's date, so fixture-driven smokes write `reading-list-2026-10-31.*`.
- (T070) ED-5's reason-order list (whitespace before `<>`/quote/`;`) contradicts its own
  example and ED-17: `Bob <b@example.com>` and `a@example.com; b@example.org` contain a
  space. config.py checks display-name characters and `;` before whitespace; test the
  specific reasons for those inputs (see implementation-notes T070).
- (T072) pyrefly infers `["must-read"] * k` as `list[str]`, so it is not assignable to
  `list[Priority]` (a Literal alias), even with the annotation. Build the list in a
  comprehension over a `tuple[tuple[Priority, int], ...]` instead.
- (T069) `EXPECTED_DELIVERY_EDGES` (the T063 positive detector) is built from
  `ONLY_PROJECT_IMPORTS`. A new `ONLY_PROJECT_IMPORTS` entry for a not-yet-written
  module therefore turns the positive test and most real-tree mutants red on the stub
  tree. Filter the derived set (`if source in DELIVERY_MODULES`) until the code exists.
- (T069) In synthetic `make_modules` trees, `from newsletter_digest import X` is an edge
  to X only if X is a key of the tree. Otherwise it counts as a package-root import, and
  importer/forbidden rules miss it. Add `"X": ""` to the case.
- (T076) `"@"` alone has exactly one `@`, so config gives "not a valid address", not the
  `@`-count reason; use `"@@"` for that. And a flag-on `safe_summary()` legitimately
  contains `@` (`gmail_mcp_args` is `@gongrzhe/...@1.1.11`): assert `@example` (or the
  addresses) is absent, never a bare `@`.
- (T073) A template-source check for the `safe` filter must be word-bounded
  (`re.search(r"\|\s*safe\b", src)`): `item.url | safe_url` contains the substring
  `| safe`. Also keep the forbidden FR-059 words (`url(`, `<img` ...) out of Jinja
  comments, because the check runs on the raw template source.
- (T071) mcp 1.30 marks `ClientSession.list_tools(cursor)` `@deprecated` (an overload,
  so there is no runtime warning, only a type-checker one). Page with
  `list_tools(params=PaginatedRequestParams(cursor=...))`. The T072 fake accepts both.
- (T071) To classify a timeout from state, not from the exception's shape, wrap only
  the `with anyio.fail_after(...)` block in `try/except TimeoutError: state.timed_out =
  True; raise`. The flag is set before the error unwinds through the MCP session's task
  groups, which can wrap it in nested `ExceptionGroup`s. A failing `client.session()`
  enter in the T072 fake logs `open#i` but no `close#i`, because nothing was opened.
  Count closes only for sessions that were entered.
- (T074) `gmail_mcp.redact_addresses` also matches the package spec
  `@gongrzhe/server-gmail-autoauth-mcp@1.1.11` (`x@y` shape). Redact only the variable
  parts of a message (details, configured values) before inserting them into fixed
  text, never the finished line, or the fixed hints read `npx <address> auth`.
- (T077) Never enter an async context manager that opens anyio task groups (an MCP
  `client.session()`, `stdio_client`) inside `with anyio.fail_after(...)` and keep it open
  after that `with` ends (e.g. via `AsyncExitStack.enter_async_context`): leaving the
  timeout scope while the inner scopes are still active raises `RuntimeError: Attempted
  to exit a cancel scope that isn't the current tasks's current cancel scope`. Put one
  `CancelScope(deadline=...)` around the whole `async with` and move its deadline
  instead. Task-group-free fakes hide this; check every timeout path once against a
  real local FastMCP stdio server.
- (T075) A lazy-import helper cannot return the lazily imported type: an `-> EmailOutcome`
  annotation needs the name at module scope (pyrefly), and `if TYPE_CHECKING:` counts as
  module level for the ED-16 rule. Type the result with a cli-private `Protocol` of
  read-only `@property` members, which a frozen dataclass satisfies. The import-failure
  branch needs its own small fallback object, because the real class failed to import
  too. Also, one-line imports quoted in task text can exceed 88 columns once indented;
  ruff wraps them, so mutant anchors must match the wrapped form.
- (T075) Run the console script (`uv run newsletter-digest --help`) from an empty
  scratch cwd with `uv run --project <dir>`, never from the project dir: cli runs
  `load_dotenv` at import time, so it may load the developer's `.env` (learnings T022).
- (T078) A wrapper's "max line <= limit" invariant needs inputs where a whitespace run
  holding a LF straddles the limit (`"a"*9 + " "*5 + "\nb"` at 10). Collapsed model text
  never produces one, so fixture renderings all pass; only a mutant that survived
  (whitespace-overflow branch loosened) showed the gap, and the new case failed on the
  real `wrap_long_lines` too (implementation-notes T078 finding 1).
- (T078) To compare an email rendering with its unwrapped form without duplicating the
  template variables, monkeypatch `email_render.to_email_ascii` / `wrap_long_lines` with
  pass-through recorders (they are looked up as module globals at call time); keep only
  `to_email_ascii` calls whose input starts with `<!doctype`, because `email_link` calls
  it too.
- (T079) A keyword default such as `retry_pause=SEND_START_RETRY_PAUSE_S` is bound when
  the function is defined, so patching the module constant does nothing. To skip a real
  pause on a default-seam path, use
  `monkeypatch.setitem(fn.__kwdefaults__, "retry_pause", 0.0)`. Callers that captured
  `fn` as their own default (`sender=send_gmail_message`) see it too.
- (T079) In "no address at any level" log checks, exempt the DEBUG `Email recipients:`
  record explicitly (ED-15 allows it). The first draft failed on it, because the check
  ran on every record.
- (T080) In CliRunner tests, `--log-level DEBUG` puts every DEBUG record on stderr
  (RichHandler), and with that the ED-15 `Email recipients:` line. So a "no address in
  the output" assertion holds only at INFO, unless `send_reading_list_email` itself is
  replaced. To test waiting send branches through the CLI (`EMAIL_SEND_TIMEOUT` >= 10 s,
  2 s retry pause), patch `email_delivery.send_reading_list_email` with
  `partial(original, sender=fast)`, where `fast` calls the real `send_gmail_message`
  with 0.05 s timeouts and a recording `sleep`. cli's lazy import picks it up.
- (T082) On this machine `env` on the agent shell's PATH is `~/.local/bin/env` (uv's
  shell snippet), not coreutils. `env X=1 cmd` silently runs nothing and exits 0, so a
  mutation runner prints no results. Always write `/usr/bin/env` (compare T067).
- (T082) A function used as a parameter default (`sender=send_gmail_message`) is bound
  when the module is imported. A tripwire on the module attribute then cannot see calls
  made through that default. Tripwire the default with `monkeypatch.setitem(fn.__kwdefaults__,
  "sender", wire)` (read `__kwdefaults__` before tripwiring `fn` itself), and assert the
  default `is` the helper.
- (T083) Do not tighten a negative architecture rule from "at most one" to "exactly
  one" when `ALLOWED_CASES` runs it on a stub tree (`STUB_EMAIL_TREE` has no cli email
  import). Put the "exactly one" rule in a positive real-tree checker, which T063
  already does for Telegram. Positive checkers also catch real gaps: no ED-3 negative
  rule reported a `gmail_mcp` send path calling `load_mcp_tools`, or a missing `if
  settings.email:` gate. Build a kill matrix (every checker × every mutant) to find
  mutants that only the positive checker catches.
- (T091) A `-p <plugin>` mutation runner whose plugin imports `tests.<module>` needs the
  project dir on the path as well as the plugin dir: `"PYTHONPATH=<scratch>:."`. With
  only the scratch dir every run ends `ModuleNotFoundError: tests` before any test, which
  looks like a "kill" if you only read the last line.
- (T091) Two redundant guards (resolving both the base dir and the returned path) make
  each single-removal mutant equivalent; mutate both together to show the tests pin the
  property, and record the single ones as equivalent rather than adding fake tests.
- (T090) A mutation probe that calls `pytest.main(..., plugins=[...])` several times in
  one process keeps the patched test module in `sys.modules`, so the failure counts add
  up across rules (11, 22, 37, 47 instead of 11, 11, 15, 10). Run one process per
  mutant. Also: when matching a helper by import alias, compare `ast.Attribute.attr`
  with the real helper name only, never with the local aliases (`x.an(...)` is not the
  helper).
- (T090) EA-2's "STUB_EMAIL_TREE and FINAL_EMAIL_TREE's email_delivery stage a file"
  cannot apply to the docstring-only stub: `POSITIVE_EMAIL_CASES` need it to fail every
  positive check. Keep the stub unchanged and run each new negative sub-rule on it
  explicitly in `ALLOWED_CASES` (implementation-notes T090).
- (T090) Never `git commit --amend` in a parallel wave: another agent committed
  (`chore: task list status`) between my commit and my amend, so the amend folded my
  test changes into its commit. Fixed with `git reset --soft <their sha>` (their commit
  restored unchanged, my changes still staged) and a separate follow-up commit. Make
  follow-ups new commits, and check `git log -1` right before any history edit.
- (T092) FastMCP (pydantic) turns `attachments: list[str] | None = None` into an
  `anyOf: [array, null]` schema, which EA-5's strict "array of strings" check refuses
  (`wrong type: attachments`). The real 1.1.11 zod schema is a plain array. A local
  test server that should mirror 1.1.11 needs `attachments: list[str] = []`.
- (T092) In a scratch probe, `monkeypatch`-style `mod.os.chmod = noop` patches the
  global `os` module, so the probe's own `os.chmod` became a no-op too and the
  "0755 directory" case falsely passed. Save the real function first and call it
  explicitly in the probe's helpers (or patch the module attribute with a proxy).
- (T092) A required dataclass field that is checked against file contents (the
  attachment must be byte-identical to `html_body`) breaks every
  `dataclasses.replace(message, html_body=...)` test that expects success: re-stage a
  copy of the new body (`ef.stage_private_copy(tmp_path, good.html_body)`) in that test.
- (T094) pyrefly checks `dataclasses.replace(obj, **mapping)` keywords against the
  field types: a `dict[str, object]` mapping fails on every field. To feed a
  wrong-typed value without a suppression, type the mapping `dict[str, Any]`.
- (T094) `ef.SEND_EMAIL_SCHEMA_1_1_11` is a read-only mapping, not a `dict`, so
  `gmail_mcp._schema_mismatch` treats it as an empty schema (`missing: <all six>`).
  The fake converts it with `dict(...)` when building the `Tool`. Call the function
  on a plain-dict copy (the test's `_schema()`).
- (T094) A "no `newsletter-digest-email-*` left in /tmp" count is meaningless while
  other agents run suites in parallel: 12 entries appeared during my run, but each
  file left 0 with `/usr/bin/env "TMPDIR=<empty scratch dir>" uv run pytest <file>`.
  Isolate the temp root before blaming a test file.
- (T094) A FIFO opened `O_RDONLY` without `O_NONBLOCK` blocks until a writer opens
  it. A test that plants a FIFO where code opens a file needs a daemon writer thread
  (`os.close(os.open(p, os.O_WRONLY))`), or the test hangs.
