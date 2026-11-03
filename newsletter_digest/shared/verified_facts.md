# Verified library facts (probed by orchestrator, 2026-10-30)

These were checked against the real packages installed from PyPI/npm in a scratch
venv. Coders should still re-verify against the versions `uv add` actually pins,
and record the results in `implementation-notes.md`.

## deepagents 0.7.32 (langchain 1.4.7, langgraph 1.2.18, langchain-core 1.7.3)
`create_deep_agent(model=None, tools=None, *, system_prompt=None, middleware=(),
subagents=None, skills=None, memory=None, permissions=None, backend=None,
interrupt_on=None, response_format=None, state_schema=None, context_schema=None,
checkpointer=None, store=None, debug=False, name=None, cache=None)`
- Parameter is `system_prompt` (str | SystemMessage), NOT `instructions`.
- `model` accepts a str or a `BaseChatModel` instance.
- `SubAgent` TypedDict keys: required `name`, `description`; optional `tools`,
  `model` (str | BaseChatModel -> yes, per-subagent model is supported),
  `middleware`, `interrupt_on`, `skills`, `permissions`, `response_format`,
  `system_prompt`, `mode` (`'isolated'` | `'fork'`).
- Subagents are invoked through the built-in `task` tool. Default deepagents
  middleware also gives the agent filesystem tools (ls/read_file/write_file/
  edit_file...), todo tool, etc. Check which built-in tools are exposed to
  subagents and whether the default backend writes to disk (prefer the in-memory
  state backend so nothing touches the real filesystem).

## langchain-mcp-adapters 0.3.2 (mcp 1.30.0)
`MultiServerMCPClient(connections: dict | None = None, *, callbacks=None,
tool_interceptors=None, tool_name_prefix=False, handle_tool_errors=True)`
- Methods: `get_tools(*, server_name=None) -> list[BaseTool]` (async),
  `session(...)`, `get_prompt`, `get_resources`, `get_server_info`.
- It is NOT an async context manager in this version: use `await client.get_tools()`.
- Stdio connection dict: `{"transport": "stdio", "command": "npx", "args": [...],
  "env": {...}}`.

## @gongrzhe/server-gmail-autoauth-mcp 1.1.11 (npm latest; node >=14)
Tool names exposed: batch_delete_emails, batch_modify_emails, create_filter,
create_filter_from_template, create_label, delete_email, delete_filter,
delete_label, download_attachment, draft_email, get_filter, get_or_create_label,
list_email_labels, list_filters, modify_email, read_email, search_emails,
send_email, update_label.  => allowlist ONLY `search_emails` and `read_email`.
- `search_emails(query: str, maxResults: number = 10 if omitted)`. NO pagination
  token, NO label arg: use Gmail query syntax, e.g. `label:Newsletters newer_than:14d`.
  Returns ONE text block, entries separated by a blank line:
  `ID: <id>\nSubject: <s>\nFrom: <f>\nDate: <d>\n` (metadata only, no body).
- `read_email(messageId: str)`. Returns one text block:
  `Thread ID: ..\nSubject: ..\nFrom: ..\nTo: ..\nDate: ..\n\n<body><attachments>`.
  Body is the text/plain part if present, else raw text/html prefixed with
  `[Note: This email is HTML-formatted. Plain text version not available.]`.
  So: HTML CAN come back raw -> strip HTML to text while preserving link URLs
  (e.g. `Title <https://...>`) before the model sees it. Plain-text bodies may also
  contain very long tracking URLs; truncating total size per email is advisable.
- Auth: `npx @gongrzhe/server-gmail-autoauth-mcp auth` once; keys at
  `~/.gmail-mcp/gcp-oauth.keys.json`, token saved to `~/.gmail-mcp/credentials.json`.
  The server's default scope includes modify/send, hence the client-side allowlist.

# Telegram PDF delivery (2026-11-02)

Probed by the design-architecture-agent on 2026-11-02 for PRD FR-037 to FR-052 (design:
`shared/designs.md` "Telegram PDF delivery (2026-11-02)").

How it was probed:
- Throwaway `uv run --no-project --with ...` environments (CPython 3.12 and 3.13.5, WSL2),
  with sockets patched to raise.
- A dry `uv add` in a scratch copy of `pyproject.toml` and `uv.lock`.
- Nothing was installed into the project, and pip was not used.

Coders must re-verify against what `uv add` pins and record the results in
`implementation-notes.md`.

## PyPI (https://pypi.org/pypi/<name>/json, 2026-11-02)
| Package | Version | Released | Licence | Notes |
|---|---|---|---|---|
| fpdf2 | **2.8.9 (pin `>=2.8.9,<2.9`)** | 2026-09-29 | LGPL-3.0-only | `py3-none-any`; requires-python >=3.10; deps Pillow (!=9.2.*, >=8.3.2), fontTools >=4.34.0, defusedxml |
| fpdf2 | 2.9.0 (not pinned) | 2026-10-29 | LGPL-3.0-only | drops defusedxml and py3.10; adds `UnicodeFontManager` (system-font suggestions); 4 days old at design time |
| pillow | 13.0.0 (resolved) | 2026-10-16 | MIT-CMU | manylinux wheel, no system packages |
| fonttools | 4.68.0 (resolved) | 2026-10-27 | MIT | pulled by fpdf2 |
| defusedxml | 0.7.1 (resolved) | 2021-03-08 | PSFL | pulled by fpdf2 2.8.x |
| pypdf | 6.21.4 (dev, `>=6.21,<7`) | 2026-10-28 | BSD-3-Clause | tests only: text and annotation extraction |
| reportlab | 5.1.0 | 2026-10-22 | BSD | fallback candidate; `Paragraph` parses XML mini-markup |
| xhtml2pdf | 0.2.28 | 2026-10-25 | Apache-2.0 | rejected (heavy; HTML/CSS) |
| weasyprint | 70.0 | 2026-09-08 | BSD | rejected (Pango system libs) |
| borb | 3.0.9 | 2026-08-03 | AGPL-3.0-or-later | rejected |
| pymupdf | 2.0 | 2026-10-19 | AGPL-3.0 / commercial | rejected (binary, onnxruntime, numpy) |
| uharfbuzz | 0.57.0 | 2026-10-23 | Apache-2.0 | not used (compiled; needed only for Arabic joining) |

Dry `uv add "fpdf2>=2.8.9,<2.9"` plus `uv add --dev "pypdf>=6.21,<7"` on the current lock
resolved 113 packages (114 with pypdf). It added only `fpdf2`, `pillow`, `fonttools`,
`defusedxml` and `pypdf`.

## fpdf2 2.8.9 behaviour (probed; 2.9.0 identical on the same probe)
- `import fpdf` takes 0.6-0.8 s and imports `PIL` and `fontTools`.
  `import newsletter_digest.cli` today loads neither (1.81 s, both absent).
- Signatures:
  - `FPDF(orientation='P', unit='mm', format='A4' | (w, h), *, enforce_compliance=None)`.
  - `multi_cell(w, h=None, text='', border=0, align=Align.J, fill=False, split_only=False,
    link=None, ln='DEPRECATED', max_line_height=None, markdown=False, print_sh=False,
    new_x=XPos.RIGHT, new_y=YPos.NEXT, wrapmode=WrapMode.WORD, dry_run=False,
    output=MethodReturnValue.PAGE_BREAK, center=False, padding=0)`.
  - `cell(..., link='', center=False, markdown=False, new_x, new_y)`.
  - `add_font(family=None, style='', fname=None, *, unicode_range=None, variations=None,
    palette=None, collection_font_number=0)`.
- Methods present: `set_fallback_fonts`, `set_text_shaping` (needs uharfbuzz),
  `set_creation_date`, `set_producer`, `start_section`, `will_page_break`,
  `set_display_mode`, `write_html`, `image`, `embed_file`, `file_attachment_annotation`,
  `alias_nb_pages`.
- **`markdown` defaults to False.** `<script>`, `**bold**`, `<b>` and `__u__` came out as
  literal text (pypdf extraction).
- **Wrapping.** A 600-character unbroken string at 11 pt on a 100 mm page (6 mm margins,
  epw 88 mm) became 17 lines, at most 85.0 mm wide. A 900-character URL-like string
  wrapped. No errors.
- **Links.** `link=` writes one `/Link` annotation with `/A <</S /URI /URI (...)>>` per
  wrapped line. Parentheses in URIs are escaped (`x\(1\)`). `javascript:alert(1)` is
  **accepted** as a URI action, so the caller must filter (`render.safe_url`).
  `start_section` adds an outline entry and no annotation.
- **Missing glyphs** do not raise. Logger `fpdf` reports
  `Font MPDFAA+DejaVuSansBook is missing the following glyphs: ...`, and the glyph is not
  drawn.
- **Bidi.** Hebrew and Arabic are reordered visually without harfbuzz: pypdf extracts the
  reversed logical string. Arabic stays unjoined.
- **Page-count alias.** `str_alias_nb_pages` defaults to `'{nb}'`, and **substitutes inside
  ordinary text**: `"Title {nb}"` was extracted as `"Title 1"`. Setting
  `pdf.str_alias_nb_pages = None` keeps it literal.
- **Determinism.** With `set_creation_date(fixed datetime)`, two builds are byte-identical.
  `/ID` is present and content-derived. Output is `%PDF-1.3`.
- **Font coverage attribute.** `pdf.fonts['<family>']` is a `TTFFont` with a `cmap` dict
  (5,918 entries for DejaVuSans).
- **No network.** No socket use during build (`connect`, `create_connection` and
  `getaddrinfo` patched to raise).
- **Speed.** 100 items (12.5 pt bold linked title, 720-character summary, meta line) built
  in 0.44 s, giving 81 pages and 124 KB.

## DejaVu fonts 2.37 (bundled)
- Source: https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip
  (sha256 `7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a`)
- `ttf/DejaVuSans.ttf`: 757,076 bytes, sha256 `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954`.
- `ttf/DejaVuSans-Bold.ttf`: 705,684 bytes, sha256 `e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724`.
- **Licence** (`dejavu-fonts-ttf-2.37/LICENSE`):
  - "Fonts are (c) Bitstream ... DejaVu changes are in public domain."
  - The Bitstream Vera licence grants permission "to reproduce and distribute the Font
    Software ... and/or sell copies".
  - Conditions: the notices must be included in all copies; modified fonts must be
    renamed (no "Bitstream" or "Vera"); "may be sold as part of a larger software package
    but no copy ... may be sold by itself".
  - The Arev glyphs are under the same style of terms.
  - **Bundling with the licence file is permitted.**
- **Cmap coverage** (fontTools `getBestCmap`; Regular and Bold the same):

| Block | Covered |
|---|---|
| Cyrillic 0400-04FF | 256/256 |
| Hebrew 0590-05FF | 54/112 |
| Arabic 0600-06FF | 165/256 |
| Arabic Presentation Forms-B | 141/144 |
| Devanagari | 0/128 |
| CJK 4E00-9FFF | 0/20,992 |
| Emoticons 1F600-1F64F | 64/80 |
| Misc Symbols and Pictographs | 12/768 |

  Present: U+FFFD, U+25A1, U+2022, U+00B7, U+2014, U+2026, U+2192, U+2605, U+2713,
  U+1F600, U+200D and U+FE0F. Absent: U+1F680 (rocket), U+0928 and U+4E2D.

## Telegram Bot API (https://core.telegram.org/bots/api, fetched 2026-11-02; latest entry "August 24, 2026 Bot API 10.3")
- Requests: "All queries to the Telegram Bot API must be served over HTTPS and need to be
  presented in this form: `https://api.telegram.org/bot<token>/METHOD_NAME`".
  - "We support GET and POST HTTP methods."
  - Parameter encodings: query string, `application/x-www-form-urlencoded`,
    `application/json` ("except for uploading files") and `multipart/form-data` ("use to
    upload files").
  - "All methods in the Bot API are case-insensitive. All queries must be made using UTF-8."
- **Response:** "a JSON object, which always has a Boolean field 'ok' and may have an
  optional String field 'description' ... In case of an unsuccessful request, 'ok' equals
  False ... An Integer 'error_code' field is also returned, but its contents are subject to
  change in the future. Some errors may also have an optional field 'parameters' of the type
  ResponseParameters".
- **`ResponseParameters`:**
  - `migrate_to_chat_id` (Integer, optional);
  - `retry_after` (Integer, optional): "In case of exceeding flood control, the number of
    seconds left to wait before the request can be repeated".
- **`sendDocument`:** "Bots can currently send files of any type of up to 50 MB in size,
  this limit may be changed in the future."
  - Params used: `chat_id` (Integer or String, required); `document` (InputFile or String,
    required; "upload a new one using multipart/form-data").
  - `caption`: "0-1024 characters after entities parsing".
  - `parse_mode` (optional) and `caption_entities` (optional) are not used.
- **Sending files:**
  - multipart: "10 MB max size for photos, 50 MB for other files";
  - by URL: "5 MB max size for photos and 20 MB max for other types";
  - a local Bot API server allows uploads "up to 2000 MB" (not used).
- **Formatting options:** `parse_mode` values are `MarkdownV2`, `HTML` and `Markdown`
  ("This is a legacy mode, retained for backward compatibility").
- **`getUpdates`:**
  - "Returns an Array of Update objects".
  - "Notes 1. This method will not work if an outgoing webhook is set up."
  - Updates "will not be kept longer than 24 hours".
- Bot FAQ (https://core.telegram.org/bots/faq): "In a single chat, avoid sending more than
  one message per second. We may allow short bursts that go over this limit, but eventually
  you'll begin receiving 429 errors."
- Bot features (https://core.telegram.org/bots/features):
  - "Use the /newbot command to create a new bot. @BotFather will ask you for a name and
    username, then generate an authentication token".
  - "Usernames are 5-32 characters long ... must end in 'bot'".
  - "Keep your token secure ... it can be used by anyone to control your bot".
  - "If your existing token is compromised or you lost it ... use the /token command to
    generate a new one."
- Telegram FAQ (https://telegram.org/faq): "Server-client encryption is used in Cloud Chats
  (private and group chats), Secret Chats use an additional layer of client-client
  encryption". Bot chats are therefore not end-to-end encrypted.
- **Not in the docs** (from experience, do not rely on it): the 400 description
  `Bad Request: chat not found`, the 403 descriptions `Forbidden: bot can't initiate
  conversation with a user` / `bot was blocked by the user`, and that the HTTP status
  equals `error_code`.

## httpx 0.28.1 / httpcore 1.0.9 (probed with MockTransport and one real request to `nonexistent.invalid`)
- **httpx logs the full URL, token included, at INFO** on logger `httpx`:
  `HTTP Request: POST https://api.telegram.org/bot<token>/sendDocument "HTTP/1.1 200 OK"`.
- httpcore logger names: `httpcore.connection`, `httpcore.http11`, `httpcore.http2`,
  `httpcore.proxy` and `httpcore.socks`. httpx has only `httpx`. The httpcore trace
  messages carry the host and port but not the path. Logger-level filters apply only to
  records created on that exact logger, so each name needs its own filter.
- `Response.raise_for_status()` produces an `HTTPStatusError` whose **message includes the
  URL** (token leaked).
- `ConnectError`, `ReadTimeout`, `WriteTimeout` and `RemoteProtocolError`: `str(exc)` and
  `traceback.format_exception(exc)` did **not** contain the token, but `exc.request.url`
  **does**. Never chain them (`__cause__`/`__context__`) into user-visible errors.
- `files={"document": (name, bytes, "application/pdf")}` plus `data={...}` sends
  `multipart/form-data; boundary=...`.
