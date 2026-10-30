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
