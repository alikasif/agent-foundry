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

### T003 — models.py

- **Who sorts:** `ReadingList` never sorts. `store.assemble_reading_list` (T004) must build
  `items` in final display order (priority, `order`, `mentioned_in` desc, `first_seen`;
  unranked: `mentioned_in` desc, `first_seen`), because `ReadingItem` carries neither
  `order` nor `first_seen`. `must_read()` and `by_topic()` preserve list order.
- **D8:** `must_read()` returns `[]` when `ranked` is False. `by_topic()` drops
  must-read items only when `ranked` is True; in the unranked fallback every item
  appears in its topic section. Topic sections follow `Topic` declaration order;
  empty topics are omitted.
- **`Topic` is a `StrEnum`** (not literally `class Topic(str, Enum)`): it is a subclass of
  both `str` and `Enum`, so the contract holds, and `str(topic)` returns the value, which
  is safer in templates and f-strings. Requires Python 3.11 (project floor).
- **`coerce_topic` matching:** the key is casefolded, `&` becomes `and`, and punctuation or
  `_` becomes a space, so `"RAG / Retrieval"`, `"rag-retrieval"`, `"DATA_ENG"` (enum name)
  and `"tools and libraries"` all match. The lookup holds the enum values, the enum names
  and `TOPIC_ALIASES`: llm/llms/large language models/language models/genai/generative ai
  -> LLMs; agent/agentic/ai agents -> Agents; rag/retrieval/search/embeddings/vector
  databases -> RAG/Retrieval; data/data eng/data engineering/data pipelines/databases/
  analytics -> Data Engineering; mlops/ml ops/infra/infrastructure/ml infra/ml
  infrastructure -> MLOps/Infra; research/paper/papers/research paper/arxiv -> Research
  Papers; industry/funding/business/startups/news -> Industry/Funding; tools/tool/
  libraries/library/frameworks/open source -> Tools & Libraries; misc/miscellaneous ->
  Other. Anything else, `None` or `""` gives `Topic.OTHER`. Exact-key lookup only, no
  substring or fuzzy matching (keeps it predictable). `coerce_topic` accepts a `Topic` and
  returns it unchanged.
- **Truncation rule (one helper, `_truncate`):** the whitespace is collapsed first
  (newlines and runs become one space). If the text is still over the limit, it is cut
  to `limit - 1` chars at the last space, as long as that keeps at least half the budget
  (otherwise mid-word, e.g. one huge token). Then trailing ` ,;:` are trimmed and `…`
  (U+2026) is appended, so the result is always `<= limit`. Used for title (300), summary
  (300), reason (200) and newsletter (120). The spec says "word boundary" only for the
  summary; the title and the reason use the same rule for consistency.
- **Truncate vs reject (deviation for `EmailSource`):** the spec gives `newsletter` 1..120
  and `date` <=64 without saying what to do when they are too long. Over-long values are
  truncated (newsletter with `…`, date with a hard cut at 64), not rejected, because a
  rejected `EmailSource` would reject the whole `record_articles` call. Empty values are
  still rejected: blank `newsletter`, `email_id`, `title`, `reason` and `article_id` raise
  a `ValidationError`.
- **URL validation (`ArticleIn.url`):** the value is stripped. It needs a `urlsplit`
  scheme in {http, https} (case-insensitive) and a non-empty `hostname`, else
  `ValueError` ("only absolute http(s) URLs with a host are allowed"). This rejects
  `javascript:`, `data:`, `mailto:`, relative, protocol-relative `//x`, `http://`,
  `https:///x`, `ftp:` and malformed IPv6 (`http://[::1`). Python 3.11+ `urlsplit` strips
  `\t\r\n`, so `java\tscript:` is still rejected. **Addition:** URLs longer than 2048
  chars are rejected. The URL is otherwise stored as given (no normalisation);
  `store.normalize_url` does that.
- **LLM-tolerance additions:** `ArticleIn.summary=None` becomes `""`. A blank
  `topic_hint` becomes `None`. `RankedItem.priority` is normalised (casefold, runs of
  spaces/underscores become `-`), so `"Must Read"`/`"must_read"` -> `"must-read"`. Unknown
  values (`"high"`) are still rejected by the `Literal`. `order` accepts numeric strings
  (pydantic lax mode) and must be >= 0. Extra keys in payloads are ignored (pydantic
  default).
- **`ReadingItem.read_key`** is a `@computed_field` (`sha1(url or id)[:16]`), not a
  stored field. The store cannot set it inconsistently, the template reads
  `item.read_key` as before, and it is included in `model_dump()`.
- **`StoredArticle.topic_hint`** and **`RankedItem.topic`** use a `mode="before"`
  validator that calls `coerce_topic`. The store can pass the raw hint string.
- **Result models:** list fields (`AddResult.rejected`, `RankResult.errors`,
  `RankResult.unranked_remaining`) default to `[]`. The other fields are as in plan.md §5.
- **Constants exported:** `ELLIPSIS`, `MAX_TITLE_CHARS`, `MAX_SUMMARY_CHARS`,
  `MAX_REASON_CHARS`, `MAX_NEWSLETTER_CHARS`, `MAX_DATE_CHARS`, `MAX_URL_CHARS`,
  `ALLOWED_URL_SCHEMES`, `TOPIC_ALIASES`.
- **Verification:** the tests belong to T011. T003 was checked with a scratch probe that
  covers: topic order, about 20 coerce_topic cases, 10 rejected URL forms, truncation
  lengths and the ellipsis, empty title/reason/id rejection, unknown priority and negative
  order rejection, unknown topic -> Other, `mentioned_in` casefold de-dupe,
  `must_read`/`by_topic` ranked vs unranked, `read_key`, and an import scan (only
  pydantic plus stdlib: re, hashlib, datetime, enum, typing, urllib.parse). `ruff format
  --check .`, `ruff check .` and `pyrefly check` (0 errors) are clean. `pytest` exits 5
  because no tests are collected yet.

### T010 — tests for config.py and llm.py

- **Files:** `tests/test_config.py` (92 tests) and `tests/test_llm.py` (7 tests); 99 pass
  offline. Coverage of `config.py` and `llm.py` is 100% (measured with
  `uv run --with pytest-cov`, so pytest-cov was not added to pyproject).
- **Env and `Settings.build`:** `build` takes explicit arguments and reads no env vars
  (env resolution is the CLI's job). The env test sets `OPENROUTER_API_KEY` etc. with
  monkeypatch and asserts `build` still uses only its arguments. CLI > env precedence
  belongs in the cli tests.
- **`make_model` without network:** the test checks `model_name`, `openai_api_base`,
  `temperature`, `max_retries`, `request_timeout` and the `SecretStr` key value on the
  constructed `ChatOpenAI`. An autouse fixture clears `OPENAI_API_KEY`/`OPENAI_API_BASE`/
  `OPENAI_BASE_URL`/`OPENROUTER_API_KEY`, and one test sets wrong env values to show that
  Settings wins.
- **"No other module constructs a chat model"** is checked with a regex over
  `src/newsletter_digest/*.py` (excluding `llm.py`) for `ChatOpenAI(`, `ChatAnthropic(`,
  `ChatOpenRouter(` and `init_chat_model(`. **"config.py imports no LangChain"** is checked
  by parsing the module's imports with AST.
- **Tests added beyond the task list:** extra invalid windows (`3w`, `2m`, `1 w`, `1.5d`,
  bare `1`, `d`, whitespace-only); words that look like date operators (`afterparty`,
  `from:before@...`) must still get `newer_than` appended, because `\b...:` requires the
  colon; the INFO log lines from D1; ConfigError messages never contain the key; the
  log-level and MCP-args ConfigError paths; and recursion_limit values around the
  100/104 boundary.
- **Bugs found:** none. Every contract in plan.md/designs.md D1/D2/D10 holds.

### T006 — render.py and template

- **CSP / hash:** `SCRIPT_BODY` is a module-level static string; `SCRIPT_HASH =
  base64(sha256(SCRIPT_BODY.encode("utf-8")))`. The template emits it as
  `<script>{{ script_body | safe }}</script>` with nothing between the tags, so the hashed
  bytes equal the emitted bytes (checked by extracting the script from rendered output and
  re-hashing). CSP is exactly the designs.md string. Inline `on*=` handlers would be
  blocked by the CSP even if escaping failed.
- **The one `|safe`:** autoescape would turn the script's `"` into `&#34;` and break it,
  so the static trusted script is marked safe in the template. `Markup` was not used
  because jinja2 3.1 no longer re-exports it and importing `markupsafe` would break the
  "render imports only jinja2 + stdlib + models" rule. No model/email data uses `|safe`.
- **`safe_url`:** removes ASCII control chars (`\x00-\x1f`, `\x7f`) anywhere, strips,
  `urlsplit`s (malformed -> `None`), and requires scheme http/https. **Stricter than spec:**
  it requires a non-empty `hostname` (not just `netloc`), so `http://:80` / `http://@`
  are rejected too. Returns the cleaned URL; Jinja escapes `&` and quotes inside `href`.
- **`slug`:** lowercase alphanumeric runs joined by `-` (`RAG/Retrieval` ->
  `rag-retrieval`, `Tools & Libraries` -> `tools-libraries`); no topic slug can collide
  with `must-read`.
- **Environment:** module-level `ENVIRONMENT` with `PackageLoader("newsletter_digest",
  "templates")`, `autoescape=True`, `StrictUndefined`, plus `trim_blocks`/`lstrip_blocks`
  for tidy output. Verified the template ships in the wheel
  (`newsletter_digest/templates/reading_list.html.j2`) and loads from the extracted wheel.
- **Template decisions / small additions:**
  - Unranked-fallback note is shown only when `not ranked` **and** there are items (the
    zero-email empty state is also unranked, and the note would be misleading there).
  - Empty state: when `emails_found == 0` the designs.md text ("No newsletter emails
    matched `<query>` in this window."); when emails were found but no articles were
    extracted, "No articles were found in the N matching emails." The nav is omitted
    when there are no items.
  - Nav and section headings show item counts. The priority pill shows `must read` /
    `worth reading` / `skim` (hyphen -> space); its class keeps the raw value.
  - Sources and date are separate flex spans (gap instead of a literal `·`), so an
    empty date leaves no dangling separator.
  - Read styling also works without JS via `.item:has(.mark-read:checked)`; the script
    adds `.is-read` and persistence under `localStorage["nd-read:" + read_key]`.
  - Dates are rendered with `isoformat()`; `generated_at` as
    `isoformat(sep=" ", timespec="minutes")` (keeps the UTC offset when aware).
- **Note for T013 (no-external-resources check):** the page contains the literal
  `http-equiv` (CSP meta, required by designs.md), so a bare `"http"` grep must exclude
  it; `http://`/`https://` appear only in item `href`s. No `<link>`, `src=`, `url(`,
  `@import` or `@font-face` occur.
- **Verification:** scratch probe (not committed; tests belong to T013) covered 17 unsafe
  URL forms, slugs for all topics, hostile title/summary/reason/newsletter/query escaping,
  link only for the safe URL with `target="_blank" rel="noopener noreferrer"`, CSP hash ==
  sha256 of the emitted script, http only in hrefs, Must read before topic sections in
  `Topic` order, unranked note/no Must read, empty state, failed-emails note, and
  `write_reading_list` (nested mkdir, `reading-list-2026-10-30.html`, same-day
  overwrite). `ruff format --check .`, `ruff check .`, `pyrefly check` (0 errors, 0
  warnings for render.py) and `pytest` (99 passed) are clean.

### T004 — store.py

Decisions not covered by the spec:
- **Heading level**: this section uses `###` under "Task notes" like the other task
  sections (the task text says `##`).
- **`TRACKER_HOSTS`** (exact host or parent domain): `list-manage.com`, `mailchi.mp`,
  `flight.beehiiv.net`, `convertkit-mail.com`, `convertkit-mail2.com`, `sendgrid.net`,
  `mandrillapp.com`, `hubspotlinks.com`, `mlsend.com`, `rs6.net`, `t.co`, `lnkd.in`,
  `bit.ly`, `tinyurl.com`, `ow.ly`, `buff.ly`. Host prefixes (`TRACKER_HOST_PREFIXES`):
  `link.`, `links.`, `click.`, `clicks.`, `email.`, `track.`, `tracking.` (designs.md
  names `link.`/`click.`/`email.`; the plural/`track` variants are common ESP hosts).
- **Tracker hosts are not "same host" evidence**: the relaxed 0.85 threshold applies only
  when the shared host is not a tracker. Every link in one beehiiv/Mailchimp issue can
  share e.g. `link.mail.beehiiv.com`, so treating that as same-host would loosen matching
  between distinct articles of one newsletter (verified: "Meta open-sources new vision
  model for robots" vs "... speech model ..." has ratio 0.889 and merges on a real
  shared host, but not on a tracker host).
- **`normalize_title`** replaces punctuation/symbols with a space (not deletion) so
  "GPT-4" becomes tokens `gpt 4` and the digit-token guard sees the version number;
  letters, digits and combining marks (Unicode categories L/N/M) are kept. `&` becomes
  ` and `.
- **`titles_match` prefilters**: the length-only bound is computed inline (identical to
  `SequenceMatcher.real_quick_ratio()`) so no matcher is built for most of the O(n)
  candidates; then `quick_ratio()` and `ratio()`. `autojunk=False` (titles are short).
  Guards run cheapest-first: min chars, length bound, min tokens, digit-token sets.
- **`normalize_url`**: an invalid port (`urlsplit(...).port` raising) is a `ValueError`
  like a bad scheme; IPv6 hosts are re-bracketed; the slash collapse skips `//` preceded
  by `:` so archive paths such as `/web/2024/https://x` keep their embedded URL; the
  query is decoded with `parse_qsl(keep_blank_values=True)` and re-encoded with
  `urlencode` (stable sort by name, so repeated params keep their relative order). The
  path itself is not re-encoded.
- **Merge bookkeeping**: every `url_key` seen for a merged article is indexed
  (`setdefault`), so a later mention via any of its URLs merges by key. When the URL is
  upgraded from a tracker to a real host, the stored host used for same-host checks is
  updated too. `merged` in `AddResult` also counts an idempotent re-record.
- **Failed vs processed**: `processed_email_ids` = recorded minus failed. An email marked
  failed and then recorded with `[]` (what the reader is told to do) stays failed; one
  that later contributes >= 1 article is un-failed. `emails_failed` in
  `assemble_reading_list` = found minus processed, which equals "failed ids + found ids
  never recorded" from designs.md, so processed + failed == found.
- **Plan addition `rankings()`**: public accessor returning applied `RankedItem`s keyed by
  article id; `assemble_reading_list` needs it and it keeps the lock encapsulated.
- **Sorting**: `items` are built in display order (models.py never sorts). Ranked:
  must-read first by `order`, then by topic (`Topic` enum order), ranked before
  unranked-skim, priority, `order`, `mentioned_in` desc, `first_seen`. Unranked fallback:
  topic order, `mentioned_in` desc, `first_seen`. A partial finalise logs a second
  WARNING with the number of unranked articles.
- **Earliest date**: parsed as RFC 2822 then ISO 8601 (naive -> UTC); the original
  display string of the earliest parseable date is kept; if none parse, the first
  non-empty date string, else "".
- **`list_rows`** clamps `offset >= 0` and `limit >= 1`; any upper cap on `limit` belongs
  to the tool layer. The truncated URL (<= 200 chars, ellipsis) is for the LLM only.

Reviewer notes:
- "Never finalised (or agent crashed with a non-empty store)": `assemble_reading_list`
  decides only from `store.finalized`; a crash after a partial `finalize_reading_list`
  therefore renders as partially ranked, which matches the FR-015 "partial" semantics.
- store.py imports the private helper `models._truncate` so row truncation matches the
  model's word-boundary/ellipsis rule exactly (one implementation).
- The design's same-host 0.85 rule can merge distinct articles on one real site
  (example above). Kept as specified; the digit-token guard and min-token/char guards
  limit it.
- Verified by a scratch smoke script (not committed; unit tests belong to the
  python_test tasks) covering every acceptance criterion: URL normalisation and
  rejection, cross-newsletter merge (`mentioned_in == 2`), tracker URL replacement, GPT-4
  vs GPT-5 and "Introduction" not merging, idempotent re-record, pagination and
  `+k more`, unknown ranking ids, partial and never-finalised assembly, and that the only
  imports are stdlib + `newsletter_digest.models`. `ruff format --check .`,
  `ruff check`, `pyrefly check` (0 errors) and `pytest` (99 passed) are clean.
