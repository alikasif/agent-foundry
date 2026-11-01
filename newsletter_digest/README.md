# newsletter-digest

`newsletter-digest` is a command line tool that reads the AI and data newsletters in
your Gmail, pulls out the individual articles, merges duplicates across newsletters,
ranks them and writes one self-contained HTML reading list.

You run it, open the file and triage: a "Must read" section comes first, followed by
topic groups (LLMs, Agents, RAG/Retrieval, Data Engineering, MLOps/Infra, Research
Papers, Industry/Funding, Tools & Libraries, Other).

Under the hood a LangChain deep agent searches Gmail through a Gmail MCP server
(read-only tools only), reads each email with a reader subagent, and ranks the
collected articles with an OpenRouter model.

## Requirements

- Python 3.11 or newer, managed with [uv](https://docs.astral.sh/uv/)
- Node.js 18 or newer (the Gmail MCP server is started with `npx`)
- A Gmail account and a Google Cloud project for the OAuth client (see below)
- An [OpenRouter](https://openrouter.ai/keys) API key

## Install

```bash
uv sync
cp .env.example .env     # then edit .env and set OPENROUTER_API_KEY
uv run newsletter-digest --help
```

## Gmail MCP setup

Gmail access goes through the community MCP server
[`@gongrzhe/server-gmail-autoauth-mcp`](https://www.npmjs.com/package/@gongrzhe/server-gmail-autoauth-mcp),
which `npx` starts for each run. The default is pinned to version 1.1.11 (see
[Pinned server version](#pinned-server-version)). You authorise it once with your own
Google OAuth client. Error messages from this tool that end with "See README: Gmail MCP
setup." point here.

### 1. Create the OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (or
   pick an existing one).
2. Open **APIs & Services > Library**, search for **Gmail API** and click **Enable**.
3. Open **Google Auth Platform > Audience** (older consoles: **APIs & Services > OAuth
   consent screen**) and configure it. Choose the **External** user type and leave the
   app in **Testing** mode. Under **Test users** click **Add users** and add the exact
   Google account whose mail you want to read. This is required even if that account
   owns the Cloud project: in Testing mode only listed test users can sign in, and any
   other account gets `Error 403: access_denied` (see
   [Troubleshooting](#troubleshooting)).
4. Open **APIs & Services > Credentials > Create credentials > OAuth client ID** and
   choose the application type **Desktop app**.
5. Download the JSON file for that client.

### 2. Save the keys file

```bash
mkdir -p ~/.gmail-mcp
mv ~/Downloads/client_secret_*.json ~/.gmail-mcp/gcp-oauth.keys.json
```

Do not leave a file named `gcp-oauth.keys.json` in the directory you run
`newsletter-digest` from. The server copies such a file into `~/.gmail-mcp` and prints a
line to stdout, which corrupts its stdio protocol and makes the run fail at startup.
Keep the file only in `~/.gmail-mcp/`.

### 3. Authorise once

```bash
npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth
```

Use the pinned version shown above; a bare `npx @gongrzhe/server-gmail-autoauth-mcp auth`
runs whatever release is newest on npm.

A browser window opens for the Google consent screen. Pick the same account you added as
a test user in step 1 (switch accounts on the picker if the browser defaults to another
one). While the app is in testing mode Google shows a "Google hasn't verified this app"
warning; click **Continue** (or **Advanced > Go to** *your app name*) and approve. The token
is saved to `~/.gmail-mcp/credentials.json`. `newsletter-digest` never reads these files;
only the MCP server does.

Note that the consent screen grants the server broad Gmail scopes (including sending and
modifying mail). See [Safety model](#safety-model) for why this tool still cannot send or
change anything.

### Custom credential locations

The server's `GMAIL_OAUTH_PATH` and `GMAIL_CREDENTIALS_PATH` environment variables are
forwarded to the server process if they are set in your environment. Apart from those,
proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` in upper and lower case) and
`NODE_EXTRA_CA_CERTS`, the MCP subprocess receives only the MCP SDK's minimal default
environment (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`). In particular
`OPENROUTER_API_KEY` is never passed to the Node process.

### Pinned server version

The default `GMAIL_MCP_ARGS` is `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, an exact
version, on purpose. The server is third-party code with broad Gmail OAuth access, and an
unpinned spec would make `npx` download and run whatever release is newest. The tool
names, argument names and output formats this tool relies on were verified against
1.1.11. If your `.env` was copied from an older `.env.example`, update its
`GMAIL_MCP_ARGS` line to the pinned value.

To upgrade:

1. Review the new release, then bump the version in `GMAIL_MCP_PACKAGE`
   (`src/newsletter_digest/config.py`) and in `.env.example` (or only in your own
   `GMAIL_MCP_ARGS` to try it locally).
2. Re-check that `search_emails` and `read_email` still exist with the same argument
   names and output format (`gmail_mcp.py` holds the mapping and the parsers).
3. Run `uv run pytest` and `uv run pytest -m integration`.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `'npx' not found on PATH; install Node.js >= 18 ...` | Install Node.js 18+ so `npx` is on your `PATH`, or set `GMAIL_MCP_COMMAND` to another launcher. |
| `Could not start the Gmail MCP server ...` or `Gmail search failed ...` | The keys file is missing or misplaced, or the token expired or was revoked. Check `~/.gmail-mcp/gcp-oauth.keys.json`, then re-run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth`. |
| Browser shows `Access blocked: <app name> has not completed the Google verification process` / `Error 403: access_denied` during `auth` | The account you signed in with is not a test user of the OAuth app. In the Cloud console open **Google Auth Platform > Audience > Test users**, click **Add users**, add that exact account, save, then re-run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` and choose that account on the picker. |
| Token expires after about a week | An OAuth app in **Testing** mode issues refresh tokens that expire after 7 days. Re-run the `auth` command, or publish the app to production in the consent screen. |
| `The Gmail MCP server does not offer the required read-only tool(s) ...` | `GMAIL_MCP_ARGS` does not point at `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, or a different server version renamed `search_emails` / `read_email`. |
| `Could not parse the Gmail MCP search_emails output` | The server's output format changed. The tool was verified against server version 1.1.11. |
| Startup fails and the working directory has `gcp-oauth.keys.json` | Remove it from the working directory (see step 2). |

## Create a "Newsletters" label with a Gmail filter

By default the tool searches the Gmail label `Newsletters`. Create it and have Gmail apply
it automatically:

1. In Gmail, open the search options (the sliders icon in the search bar).
2. Enter the newsletter senders in **From**, separated by `OR`, for example
   `news@example.com OR digest@example.org`. Click **Create filter**.
3. Tick **Apply the label**, choose **New label...** and name it `Newsletters`.
4. Optionally tick **Skip the Inbox (Archive it)** to keep your inbox clean.
5. Tick **Also apply filter to matching conversations** so existing messages in the
   time window are labelled too, then click **Create filter**.

To add more senders later, open **Settings > Filters and Blocked Addresses**, edit the
filter and extend the **From** list.

You do not need the label. Use `--label "My Label"` for a different label, or `--query`
for any Gmail search (see below).

## Configuration

Settings come from CLI flags, environment variables and `.env` (loaded from the current
directory). Precedence is **CLI flag > environment variable > default**. A variable that
is already set in your shell wins over the same variable in `.env`.

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | none (required) | OpenRouter API key. Read from the environment only. |
| `OPENAI_API_BASE` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL for all chat models. Environment only. |
| `NEWSLETTER_MODEL` | none (required, unless `--model` is given) | Model for the main (ranking) agent. `.env.example` sets `anthropic/claude-sonnet-4.5`. |
| `NEWSLETTER_READER_MODEL` | value of `NEWSLETTER_MODEL` | Cheaper model for the per-email reader subagents. |
| `NEWSLETTER_LABEL` | `Newsletters` | Gmail label to search. |
| `GMAIL_MCP_COMMAND` | `npx` | Command that launches the Gmail MCP server. Environment only. |
| `GMAIL_MCP_ARGS` | `@gongrzhe/server-gmail-autoauth-mcp@1.1.11` | Arguments for that command, parsed with shell-style quoting. Environment only. |

Blank values count as unset. `GMAIL_OAUTH_PATH`, `GMAIL_CREDENTIALS_PATH`, proxy
variables and `NODE_EXTRA_CA_CERTS` are forwarded to the MCP server if set (see
[Gmail MCP setup](#gmail-mcp-setup)).

### CLI options

```text
uv run newsletter-digest [OPTIONS]
```

| Option | Env var | Default | Description |
| --- | --- | --- | --- |
| `--window TEXT` | none | `1w` | Time window: `1w` (7 days), `2w` (14), `1m` (30) or `Nd` with N from 1 to 365 (for example `10d`). |
| `--label TEXT` | `NEWSLETTER_LABEL` | `Newsletters` | Gmail label to search. Ignored when `--query` is given. |
| `--query TEXT` | none | none | Custom Gmail search query that replaces the label. |
| `--max-emails INTEGER` | none | `60` | Maximum number of emails to process, 1 to 200. |
| `--model TEXT` | `NEWSLETTER_MODEL` | none | OpenRouter model for the main agent. |
| `--reader-model TEXT` | `NEWSLETTER_READER_MODEL` | same as `--model` | OpenRouter model for the per-email reader. |
| `--out DIRECTORY` | none | `out` | Output directory for `reading-list-YYYY-MM-DD.html` (created if needed). |
| `--open` | none | off | Open the reading list in the default browser. |
| `--log-level [debug\|info\|warning\|error]` | none | `INFO` | Logging verbosity. `DEBUG` also prints tracebacks on failure. |
| `-h`, `--help` | none | none | Show help and exit. |

### How `--window`, `--label` and `--query` combine

- Without `--query` the Gmail query is `label:<label> newer_than:<days>d`. A label that
  contains spaces is quoted, for example `label:"My News" newer_than:14d`.
- `--query` replaces the label part. `newer_than:<days>d` is appended to it, unless the
  query already contains one of `newer_than:`, `older_than:`, `after:` or `before:`. In
  that case your query is used exactly as written and `--window` only affects the date
  range shown in the page header.
- If both `--label` and `--query` are given, `--label` is ignored and an INFO note says so.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success, including an empty result and an unranked fallback list. |
| `1` | Runtime failure: Gmail MCP launch, auth or search error, or an agent failure. Re-run with `--log-level DEBUG` for the traceback. |
| `2` | Usage or configuration error: missing `OPENROUTER_API_KEY` or model, invalid window, `--max-emails` out of range, unparsable `GMAIL_MCP_ARGS`. |

Logs, the summary line (`<n> articles from <processed> of <found> emails`) and warnings go
to stderr. stdout carries only the path of the written file.

## Usage

```bash
# Last week of the Newsletters label
uv run newsletter-digest

# Last two weeks
uv run newsletter-digest --window 2w

# Last week, at most 5 emails, open the result in the browser
uv run newsletter-digest --window 1w --max-emails 5 --open

# One sender instead of the label (newer_than:7d is appended)
uv run newsletter-digest --query 'from:news@example.com'

# A different label
uv run newsletter-digest --label "AI Reading"

# A stronger model for ranking and a cheaper one for reading each email
uv run newsletter-digest \
  --model anthropic/claude-sonnet-4.5 \
  --reader-model anthropic/claude-haiku-4.5

# Print the output path only, and open it with your own opener
xdg-open "$(uv run newsletter-digest --window 2w)"
```

Try `--max-emails 5` first to check the setup cheaply before a full run.

## Output

The tool writes `<out>/reading-list-YYYY-MM-DD.html`, a single file with inline CSS and
no external requests. It supports light and dark mode. It contains:

- **Header:** window, date range, and counts of emails found, processed and failed,
  plus the article count. Emails that could not be read are skipped and counted.
- **Must read:** the top-priority articles come first.
- **Topic groups:** the remaining articles grouped by topic. Each entry has a linked
  title, a one or two line summary, the source newsletter(s), the date and the reason
  for its priority (must read, worth reading or skim).
- **"mentioned in N" badge:** shown when the same article appeared in more than one
  newsletter. Duplicates are merged on the normalised URL (tracking parameters and
  fragments removed) plus a fuzzy title match.
- **Mark read checkbox:** each entry has a checkbox whose state is stored in the browser's
  `localStorage`. This is best effort; the page works if storage is unavailable, and
  nothing is stored outside your browser.
- **Unranked fallback:** if the agent does not finish ranking, the list is still written,
  grouped by suggested topic with no Must read section. The page shows a note and the CLI
  prints a warning. The exit code is still 0.
- **Empty state:** if no emails match, a short page says so and shows the query that was
  used. No model call is made, and the exit code is 0.

## Safety model

The tool is designed so that a run can read your newsletters and nothing else.

- **Read-only allowlist, enforced client-side.** The MCP server offers 19 tools,
  including ones that send, modify, label and delete mail. `gmail_mcp.py` keeps only
  `search_emails` and `read_email` and discards the rest (default-deny). The model never
  sees the other tools. If an allowlisted tool is missing, the run stops with an error.
- **The OAuth scope is not the guard.** The token you create in step 3 comes from the
  server's own consent flow, and its scopes include send and modify access. The allowlist
  in this tool is the only thing preventing writes. Anyone who runs the MCP server
  directly, or changes the allowlist, has that access. Treat `~/.gmail-mcp/` accordingly.
- **Reads are limited to the search results.** The reader can only open email ids that
  came from this run's search; any other id is refused without calling Gmail.
- **Credentials stay with the server.** Tokens and keys live in `~/.gmail-mcp/` and are
  read only by the MCP server. This tool does not read them, and the API key is excluded
  from logs and from the MCP subprocess environment.
- **The server version is pinned.** The MCP server is third-party code that holds your
  Gmail token, so the default `GMAIL_MCP_ARGS` names an exact release instead of letting
  `npx` fetch the latest one on every run. See
  [Pinned server version](#pinned-server-version).
- **Email content is untrusted data.** Each email is stripped of HTML, hidden elements
  and zero-width characters, truncated, and wrapped in markers that tell the model it is
  data and not instructions. The prompts tell the model to ignore instructions found in
  emails, and the model's tools are limited to recording articles and reading listed
  emails. Delimiter look-alikes inside an email are removed so it cannot close the wrapper.
- **Prompt injection is mitigated, not eliminated.** No prompt-level defence is
  guaranteed against a determined attacker. What bounds the damage is the tool surface: a
  hostile newsletter could at worst distort the summaries, titles, links or ranking in
  your reading list. It cannot send mail, modify Gmail or reach other tools, because
  those are not available to the model. Treat links in the list as you would any link
  from an unknown sender.
- **The HTML output is escaped.** All dynamic content is HTML-escaped, links are limited to
  `http` and `https`, and the page has a Content-Security-Policy that blocks external
  resources and permits only its own inline script.
- **Known residual risk.** The agent framework also adds a default `general-purpose`
  subagent that this tool cannot switch off. The system prompt forbids using it. If a
  model delegates to it anyway it gets no Gmail access beyond the same listing and
  recording tools, so it wastes tokens but gains no extra privilege.

## Privacy

- **Email content is sent to the configured OpenRouter model.** The text of every
  processed email (headers Subject, From and Date, and the body, truncated to 30,000
  characters) is sent to `NEWSLETTER_MODEL` and `NEWSLETTER_READER_MODEL` through
  OpenRouter, and from there to the model provider. Choose models and a provider whose
  data policy you accept, and use a narrow label or query if some mail should not leave
  your machine.
- **The output contains only derived data:** article titles, links, summaries, newsletter
  names and dates. It does not contain full email bodies.
- **Credentials** stay in `~/.gmail-mcp/` and are never read or logged by this tool.
- The tool keeps no database or history. The only persisted state is the HTML file and
  the mark-read flags in your browser.

## Development

```bash
uv sync
uv run pytest                    # unit tests (integration tests are deselected)
uv run pytest -m integration     # needs Gmail OAuth, npx and OpenRouter access
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
```

Source lives in `src/newsletter_digest/`:

| Module | Purpose |
| --- | --- |
| `cli.py` | `newsletter-digest` command, exit codes, logging |
| `config.py` | `Settings`, window parsing, Gmail query building |
| `gmail_mcp.py` | MCP connection, read-only allowlist, email text preparation |
| `tools.py` | Agent tools: `search_emails`, `read_email` wrapper, `record_articles`, `list_collected`, `finalize_reading_list` |
| `agent.py`, `prompts.py`, `llm.py` | Deep agent assembly, prompts, model factory |
| `models.py`, `store.py` | Pydantic models, in-memory article store and de-duplication |
| `render.py`, `templates/` | HTML reading list rendering |

## Limitations

- Gmail search returns one page: there is no pagination, so at most `--max-emails` (up to
  200) of the matching emails are processed.
- The Gmail MCP integration was checked against `@gongrzhe/server-gmail-autoauth-mcp`
  1.1.11, the pinned default, by reading its source and with a fake server, not against
  a live Gmail account. Other versions (set through `GMAIL_MCP_ARGS`) may change tool or
  output formats.
- Emails are truncated to 30,000 characters each; very long newsletters may lose their
  tail.
- Extraction, de-duplication and ranking depend on the model, so results can vary between
  runs and models.
- Testing-mode OAuth tokens expire after about 7 days (see Troubleshooting).
- Mark-read state is stored per browser (keyed by article URL) and is not synced.
