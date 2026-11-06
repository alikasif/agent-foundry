# newsletter-digest

`newsletter-digest` is a command line tool that reads the AI and data newsletters in
your Gmail, pulls out the individual articles, merges duplicates across newsletters,
ranks them and writes one self-contained HTML reading list.

You run it, open the file and triage: a "Must read" section comes first, followed by
topic groups (LLMs, Agents, RAG/Retrieval, Data Engineering, MLOps/Infra, Research
Papers, Industry/Funding, Tools & Libraries, Other).

Optionally (`--fetch-articles`, off by default) it also fetches the linked pages of your
top items and summarises them; see
[Fetching article pages](#fetching-article-pages---fetch-articles). Optionally
(`--telegram`) it also sends a PDF copy to your own Telegram chat; see
[Telegram delivery](#telegram-delivery). Optionally (`--email`) it also emails the
reading list to addresses you choose; see [Email delivery](#email-delivery).

Under the hood a LangChain deep agent searches Gmail through a Gmail MCP server
(the agent gets read-only tools only), reads each email with a reader subagent, and
ranks the collected articles with an OpenRouter model. Only with `--email` does the
app's own delivery code (not the agent) call the server's send tool, in a separate
step after the HTML is written; see [Email delivery](#email-delivery).

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
| `The Gmail MCP server does not offer the required read-only tool(s) ...` | `GMAIL_MCP_ARGS` does not point at `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, or a different server version renamed `search_emails` / `read_email` (the two tools the agent's read-only session uses). Problems with the send tool used by `--email` are listed under [Troubleshooting email](#troubleshooting-email). |
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
| `NEWSLETTER_FETCH_ARTICLES` | off | Turns on article fetching, same as `--fetch-articles`. |
| `NEWSLETTER_MAX_FETCH` | `15` | Same as `--max-fetch`, 1 to 50. |
| `NEWSLETTER_FETCH_TIMEOUT` | `15` | Overall seconds per page, 1 to 120. Environment only. |
| `NEWSLETTER_FETCH_CONNECT_TIMEOUT` | `5` (or the fetch timeout if that is lower) | Connect seconds, 0.5 up to the fetch timeout. Environment only. |
| `NEWSLETTER_FETCH_MAX_REDIRECTS` | `5` | Redirects followed per page, 0 to 10. Environment only. |
| `NEWSLETTER_FETCH_MAX_BYTES` | `2097152` (2 MiB) | Maximum page size read, 65536 to 20971520. Environment only. |
| `NEWSLETTER_FETCH_MAX_CHARS` | `20000` | Maximum characters of extracted text kept per page, 1000 to 100000. Environment only. |
| `NEWSLETTER_FETCH_CONCURRENCY` | `4` | Concurrent page fetches, 1 to 16. Environment only. |
| `NEWSLETTER_FETCH_LLM_CONCURRENCY` | `2` | Concurrent summary calls, 1 to 8. Environment only. |
| `NEWSLETTER_FETCH_LLM_INTERVAL` | `3.1` if the reader model name ends in `:free`, otherwise `0` | Minimum seconds between the starts of summary calls, 0 to 60. An explicit value, including `0`, wins. Environment only. |
| `TELEGRAM_BOT_TOKEN` | none (required with `--telegram`) | Bot token from @BotFather. Read only when `--telegram` is given; never printed or logged. Environment only. See [Telegram setup](#telegram-setup). |
| `TELEGRAM_CHAT_ID` | none (required with `--telegram`) | Your own numeric chat ID (a positive integer). Read only when `--telegram` is given. Environment only. |
| `EMAIL_TO` | none (required with `--email`) | Comma-separated bare addresses to email the digest to: at most 10, duplicates removed. Read only when `--email` is given. Environment only. See [EMAIL_TO](#email_to). |
| `EMAIL_SEND_TIMEOUT` | `90` | Seconds to wait for Gmail to accept the send, 10 to 600. Read only when `--email` is given. Environment only. |

Blank values count as unset. The two `TELEGRAM_*` variables are ignored unless
`--telegram` is given; with it, a missing, blank or malformed value exits with code 2
before any Gmail or model work. `EMAIL_TO` and `EMAIL_SEND_TIMEOUT` are likewise read
and validated only with `--email`, with the same exit code 2 and timing. The fetch variables are validated on every run, even with
fetching off; an invalid value exits with code 2. `GMAIL_OAUTH_PATH`, `GMAIL_CREDENTIALS_PATH`, proxy
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
| `--open` | none | off | Open the reading list in the default browser. On WSL it opens in the Windows default browser (see below). |
| `--fetch-articles` | `NEWSLETTER_FETCH_ARTICLES` | off | Also fetch the linked pages of must-read and worth-reading items and summarise them. See [Privacy](#privacy). |
| `--max-fetch INTEGER` | `NEWSLETTER_MAX_FETCH` | `15` | Maximum number of article pages to fetch, 1 to 50. Only used with `--fetch-articles`. |
| `--telegram` | none (flag only) | off | Also save a PDF copy next to the HTML and send it to your own Telegram chat. Needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. See [Telegram delivery](#telegram-delivery) and [Privacy](#privacy). |
| `--email` | none (flag only) | off | Also email the reading list (HTML plus a short plain-text part) to the addresses in `EMAIL_TO`, through the same Gmail MCP server and login. No SMTP, no PDF. Combines with `--telegram`. See [Email delivery](#email-delivery) and [Privacy](#privacy). |
| `--log-level [debug\|info\|warning\|error]` | none | `INFO` | Logging verbosity. `DEBUG` also prints tracebacks on failure. |
| `-h`, `--help` | none | none | Show help and exit. |

On WSL (detected from `WSL_DISTRO_NAME`, `WSL_INTEROP` or `/proc/version`), `--open`
uses `wslview` if it is installed (package `wslu`), otherwise `explorer.exe` on the
file's Windows path, and only then Python's `webbrowser`. If none of them works, a
one-line hint with the file path is printed to stderr; the exit code is unchanged.

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
| `2` | Usage or configuration error: missing `OPENROUTER_API_KEY` or model, invalid window, `--max-emails` out of range, unparsable `GMAIL_MCP_ARGS`. With `--telegram`: `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` missing, blank or malformed. With `--email`: `EMAIL_TO` or `EMAIL_SEND_TIMEOUT` missing or invalid. Both are checked before any work starts. |
| `3` | Only with `--telegram` and/or `--email`: the digest was written (HTML, and the PDF when it was built) but at least one requested channel did not deliver it, or the email outcome is unknown. A one-line reason per channel on stderr says why; see [Troubleshooting Telegram](#troubleshooting-telegram) and [Troubleshooting email](#troubleshooting-email). |

Logs, the summary line (`<n> articles from <processed> of <found> emails`) and warnings go
to stderr. stdout carries only the paths of the written files: the HTML path, and with
`--telegram` the PDF path on a second line whenever the PDF file exists. `--email` adds
no stdout line; its result is one `Email:` line on stderr.

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

## Fetching article pages (`--fetch-articles`)

By default the summaries in the list come from the newsletter emails and no linked page is
opened. With `--fetch-articles` the tool also downloads the page behind each top item,
extracts its text and asks the reader model for a short summary. With the flag off nothing
changes: no extra network or model calls, and the same HTML as before.

```bash
# Fetch up to 15 pages (the default) for the must-read and worth-reading items
uv run newsletter-digest --fetch-articles

# A small first run, as suggested for free-tier models
uv run newsletter-digest --max-emails 5 --fetch-articles --max-fetch 5 --open
```

Read [Privacy](#privacy) first: fetching contacts third-party sites and follows tracking
links.

### Which items are fetched

- Only items ranked **must read** or **worth reading**; **skim** items are never fetched.
- At most `--max-fetch` pages (default 15, range 1 to 50), taking must-read items first,
  then by ranking order and how many newsletters mentioned the item.
- Links that are not articles (unsubscribe, manage preferences, view in browser, social,
  tracking pixels, sponsor and ad links) are never requested. Click-tracking redirect
  links are followed.
- Some click trackers (HubSpot, used by Towards Data Science, for example) answer with a
  small "You're being redirected" page instead of a redirect. When a small HTML page gives
  almost no text and is such a page (or a `<meta http-equiv="refresh">` page with a
  delay of at most 10 seconds), its single onward link is followed once, with the same
  safety checks as any redirect. That hop counts towards the redirect limit and the
  per-page time limit. A page that only points back at itself is not followed.
- If ranking did not finish (the unranked fallback), nothing is fetched and the page header
  says "Article fetching was skipped: ranking was not completed."
- Each URL is requested once per run; a second item that resolves to the same page keeps
  its newsletter summary without a note.

### What the output shows

For a successfully fetched item:

- the title link is unchanged (it still points at the original link);
- a summary of about 3 to 5 sentences and 3 to 5 key points from the model, **replacing**
  the newsletter blurb;
- a collapsed **Read here** section with the extracted page text, up to 20,000 characters
  (`NEWSLETTER_FETCH_MAX_CHARS`). Longer text ends with a `[…truncated]` marker.

The newsletter blurb is dropped only when the fetch **and** the summary both succeed. If
either fails, the blurb stays and a note explains why. The header gains an "Article pages"
line (fetched and summarised of selected, how many were eligible, how many could not be
fetched) and the footer explains that items with key points were summarised from the page.

### Notes on items that could not be fetched

The note reads `Couldn't fetch the article: <reason>`. For the two model-side reasons it
reads `Couldn't summarise the article: <reason>`.

| Note text | Meaning |
| --- | --- |
| `paywalled or login required` | HTTP 401 or 402, a redirect to a login or subscribe path, or a page marked as not free with almost no text. |
| `blocked by the site` | HTTP 403, 429 or 451, or a bot-protection challenge page. |
| `timed out` | The page did not answer within `NEWSLETTER_FETCH_TIMEOUT`. |
| `network or TLS error` | DNS, connection, TLS or protocol failure. |
| `page unavailable` | Any other non-success HTTP status (404, 410, 5xx). |
| `not a web page` | Not HTML (PDF, image, JSON) or an unsupported content encoding. |
| `too large` | The page exceeded `NEWSLETTER_FETCH_MAX_BYTES`. |
| `no readable article text` | No main text could be extracted, or under 400 characters (often a JavaScript-only page). |
| `address not allowed` | The link failed the safety checks (see [Safety model](#safety-model)). |
| `not an article page` | A redirect landed on an unsubscribe, preferences or view-in-browser page. |
| `too many redirects` | More than `NEWSLETTER_FETCH_MAX_REDIRECTS` redirects (a followed tracker page counts as one), or a redirect loop. |
| `couldn't summarise` | The model call failed (not a rate limit) or returned unusable output. Shown as "Couldn't summarise the article". |
| `model rate limit reached` | The model returned HTTP 429 after retries, so remaining items were not tried. Shown as "Couldn't summarise the article". |
| `couldn't fetch` | Any other unexpected error. |

### Cost and free-tier limits

- One reader-model call per fetched page, at most `--max-fetch` in total, using
  `--reader-model` (or `--model` if unset).
- Defaults: 4 page fetches at a time and 2 model calls at a time
  (`NEWSLETTER_FETCH_CONCURRENCY`, `NEWSLETTER_FETCH_LLM_CONCURRENCY`).
- OpenRouter `:free` models (for example `xingchen-agi/xing4.0-29b-a4b:free`) allow 20
  requests per minute, and 50 requests per day for accounts that have bought fewer than 10
  credits (1000 per day with 10 or more). The agent run itself (one reader call per email
  plus the ranking calls) can use up the daily allowance before fetching starts.
- When the reader model name ends in `:free`, summary calls are paced at least 3.1 seconds
  apart (`NEWSLETTER_FETCH_LLM_INTERVAL`, which also applies to other models if you set it).
- After a 429 that survives the client's retries (3), a circuit breaker stops further
  summary calls: the remaining items show `model rate limit reached` and keep their blurbs.
  The run still finishes with exit code 0.
- On a free tier start small, for example `--max-emails 5 --max-fetch 5`.

### Limitations of fetching

- No JavaScript rendering: pages that build their text in the browser give `no readable
  article text`.
- No PDFs or other non-HTML content.
- No login, paywall or bot-protection bypass, and no cookies are kept or sent.
- No cache across runs: every run fetches again.
- Proxy environment variables (`HTTP_PROXY`, `HTTPS_PROXY`) are ignored by the fetcher, so
  fetching does not work where a proxy is mandatory.
- The extraction library (trafilatura) is imported when the tool starts, which adds a
  little start-up time even with the flag off.

## Telegram delivery

With `--telegram`, the tool also builds a PDF copy of the same reading list and sends it
to your own Telegram chat through a bot you create. The HTML file is written first and is
never changed by this step. Without the flag nothing Telegram-related is loaded, read or
sent, and the HTML output is unchanged.

```bash
uv run newsletter-digest --window 1w --telegram
```

### Telegram setup

You need a Telegram account, a bot you own and your chat ID. About five minutes, once.

1. **Create the bot.** In Telegram, open **@BotFather** and send `/newbot`. Give a display
   name, then a username that ends in `bot` (5 to 32 characters: Latin letters, digits and
   underscores). BotFather replies with the bot token, which looks like
   `<digits>:<letters, digits, _ or ->`.
   - **Keep the token secret.** Anyone with it can act as your bot. If it leaks, send
     `/token` to BotFather to generate a new one, then update `.env`.
2. **Save the token.** Add it to `.env` in the directory you run the tool from (see
   `.env.example`, where the Telegram lines are commented out):

   ```text
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   ```

3. **Press Start.** Open `https://t.me/<your_bot_username>` and press **Start** (this
   sends `/start`). A bot cannot message you until you do.
4. **Find your chat ID.** The one-liner below reads the token from `.env`, so it does not
   end up in your shell history or in `ps` output. It prints the `message.chat.id` values
   seen in the bot's recent messages:

   ```bash
   uv run python -c "import os, httpx, dotenv; dotenv.load_dotenv(); t = os.environ['TELEGRAM_BOT_TOKEN']; print({u['message']['chat']['id'] for u in httpx.get(f'https://api.telegram.org/bot{t}/getUpdates').json()['result'] if 'message' in u})"
   ```

   The number is a positive integer for a private chat.
   - **Alternative with curl.** `read -rs` keeps the token off the command line and out of
     history. Read `result[].message.chat.id` in the output:

     ```bash
     read -rs TELEGRAM_BOT_TOKEN; curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
     ```

   - **Do not paste the URL, the command output or your terminal log into shared places.**
     The URL contains your token, and the output contains your chat ID.
   - **Empty result (`set()` or `"result": []`)?** Send `/start` (or any message) to the
     bot again and re-run. Telegram keeps updates for at most 24 hours, and returns none
     while a webhook is set for the bot.
5. **Save the chat ID.** Add it to `.env`:

   ```text
   TELEGRAM_CHAT_ID=<the number>
   ```

   Only your own private chat ID (a positive number) is accepted. Group and channel IDs
   (negative numbers) are not supported.
6. **Run it.** `uv run newsletter-digest --window 1w --max-emails 5 --telegram`

### What is sent

- **The PDF** is saved as `<out>/reading-list-YYYY-MM-DD.pdf`, beside the HTML (a PDF
  from an earlier run on the same date is replaced), and sent to your chat as a document.
  It is laid out for a phone screen (100 x 178 mm pages) with the same sections as the
  HTML: header counts, must read, then topic groups, each item with a linked title,
  summary, newsletter names, date and priority. Fetched article text ("Read here") is not
  included.
- **The caption** is plain text built from the window and counts only, never titles or
  email text:
  - `Newsletter digest: last 1w - 12 articles, 3 must-read`
  - `Newsletter digest: last 1w - 12 articles, unranked` (the ranking fell back)
  - `Newsletter digest: last 1w - no articles found`
- **Console output.** stdout gets the HTML path, then the PDF path. stderr gets
  `Sent reading-list-YYYY-MM-DD.pdf to Telegram.` on success. On failure it gets one line,
  `Telegram delivery failed (<reason>). Saved: <files>. See README > Telegram delivery.`,
  and the exit code is 3. The digest files are kept either way.
- **Retries.** Timeouts, network errors, server errors (5xx) and short rate limits (429
  with a wait of up to 30 seconds) are retried, at most 3 attempts within 120 seconds.

### PDF limits

- **Size:** Telegram bots can upload files up to 50 MB. The tool checks 50,000,000 bytes
  and does not upload a larger PDF (exit 3, the PDF stays on disk). Typical PDFs are far
  smaller.
- **Fonts:** the PDF uses the bundled DejaVu Sans (regular and bold). Characters it lacks,
  for example CJK (Chinese, Japanese, Korean) and Devanagari, are shown as replacement
  marks. There is no fallback font in this version. The title link still works.
- **Arabic** is shown with unjoined letters (no shaping), and **colour emoji** are not
  drawn.

### Licences

The PDF is built with [fpdf2](https://github.com/py-pdf/fpdf2), which is licensed under
the LGPL-3.0 and is used as an unmodified library dependency. The bundled DejaVu Sans fonts
are under the Bitstream Vera / DejaVu licence; the licence text ships with the fonts
(`src/newsletter_digest/fonts/LICENSE`, also included in the built wheel).

### Troubleshooting Telegram

Configuration errors exit with code 2 and name the variable; they end with
`See README > Telegram delivery > Telegram setup.` Delivery problems exit with code 3.

| Message (shortened) | Cause and fix |
| --- | --- |
| `TELEGRAM_BOT_TOKEN ... not set` / `does not look like a bot token` | Add or fix the token in `.env` (exit 2). The format is `<digits>:<letters, digits, _ or ->`. |
| `TELEGRAM_CHAT_ID must be your own numeric chat ID` | Use the positive number from step 4, not a group or channel ID (exit 2). |
| `invalid bot token` (HTTP 401) or `bot token not recognised` (HTTP 404) | The token is wrong, revoked or was regenerated. Copy it again from BotFather (`/token`). |
| `HTTP 400: chat not found ...` | `TELEGRAM_CHAT_ID` is wrong, or you never pressed **Start** in the bot chat. |
| `the bot may not message this chat` (HTTP 403) | Press **Start** in your bot chat, or unblock the bot. |
| `HTTP 429: rate limited` | Telegram asked to slow down. Short waits are retried automatically; if it still fails, wait and re-run. |
| `the PDF is ... bytes ..., over Telegram's 50 MB upload limit; not sent` | Use a shorter `--window` or a lower `--max-emails`. The PDF stays in the output directory. |
| `gave up after 120 s` / `timed out` / `network error` | Check your connection and re-run. |
| `could not build the PDF` / `could not write the PDF` | The HTML is still written. Re-run with `--log-level DEBUG`; check that the output directory is writable. A PDF from an earlier run the same day may still be there, and the message says so. |

**Possible duplicate message.** If the upload times out after Telegram already received
the file, the tool retries and you may get the same PDF twice. The tool cannot tell
whether the first attempt arrived, so it accepts a possible duplicate rather than risk
sending nothing.

Re-running the command with `--telegram` also sends the digest again.

### Trying it end to end

With a real bot and your chat ID in `.env`:

1. Run `uv run newsletter-digest --window 1w --max-emails 5 --telegram`.
2. Check stdout: two lines, the `.html` path and the `.pdf` path. Check that stderr ends
   with `Sent reading-list-<date>.pdf to Telegram.` and that the exit code is 0
   (`echo $?`).
3. In Telegram, open the bot chat: a PDF document with a counts-only caption should be
   there. Open it on your phone and check the layout and the links.
4. Failure check: set a wrong `TELEGRAM_CHAT_ID` in `.env`, re-run, and confirm the
   exit code is 3 and that stderr names the cause. Restore the correct value afterwards.
   Never paste the token into the terminal, an issue or a log.

## Email delivery

With `--email`, the tool also emails the reading list to the addresses you list in
`EMAIL_TO`. The message is one HTML email plus a short plain-text part. It is sent through
the same pinned Gmail MCP server and the same login as the search: no SMTP, no new
credentials, no PDF. The HTML file is written first and is never changed by this step.
Without the flag nothing email-related is loaded, read or sent, and the HTML output is
unchanged. It combines with `--telegram`: both channels are attempted.

```bash
uv run newsletter-digest --window 1w --email
uv run newsletter-digest --window 1w --telegram --email
```

### EMAIL_TO

Put the recipients in `.env` (they are never taken from the command line):

```text
EMAIL_TO=you@example.com
# several recipients
EMAIL_TO=you@example.com,colleague@example.org
# optional: seconds to wait for Gmail to accept the send (10-600, default 90)
EMAIL_SEND_TIMEOUT=90
```

- Comma-separated **bare** addresses, at most **10**. Duplicates are removed (compared
  without regard to case); spaces around a comma and a trailing comma are fine.
- Rejected, with exit code 2 before any Gmail or model work: display names and angle
  brackets (`Bob <bob@example.org>`), semicolons as separators, spaces inside an address,
  non-ASCII addresses, CR/LF or other control characters, an entry that is not a plain
  `name@domain.tld` address, and more than 10 unique addresses. The message names the
  offending entry (shortened to 32 characters) and says why.
- `EMAIL_TO` and `EMAIL_SEND_TIMEOUT` are read only with `--email`. A missing or blank
  `EMAIL_TO`, or an `EMAIL_SEND_TIMEOUT` outside 10 to 600, exits with code 2.
- Every recipient is in the To line and sees the others. There is no Cc, Bcc or
  attachment.

### Security model

- The AI agent still has only `search_emails` and `read_email`, behind the same
  default-deny allowlist. It never sees `send_email`.
- The app's own deterministic delivery code calls `send_email` in a **separate, later,
  short-lived** Gmail MCP session, after the HTML is written and the agent session has
  ended. The recipients come from `EMAIL_TO`, and the subject is a fixed template built
  from counts only (see below).
- No other Gmail write tool (draft, modify, delete, label, filter) is used by anything.
- The Gmail session the agent uses stays read-only; the send session is never given to a
  model.

### OAuth scope

The server's `auth` command requests the `gmail.modify` and `gmail.settings.basic`
scopes. `gmail.modify` already permits sending, so for a token made as described in
[Gmail MCP setup](#gmail-mcp-setup) no scope change and no re-authorisation is expected.
The Gmail MCP token must permit sending: if it was created with a read-only scope, the
send fails with a permission error and you re-run the auth:

```bash
npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth
```

The first real send is what confirms the token has the scope; nothing checks it earlier.

### What is in the email

- **Subject** (counts only, printable ASCII, never a title or any model text):
  `Newsletter digest: last 1w (2026-11-04) - 24 articles, 5 must-read`. An unranked list
  reads `... - 24 articles, unranked`; an empty one `... - no articles found`. Empty lists
  are emailed too.
- **Body:** the window and counts header, the Must read section first, then the topic
  groups. Each item has its title as a link, the summary (the fetched-page summary and
  key points when `--fetch-articles` produced them), newsletter names, date, "mentioned
  in N" when several newsletters carried it, and the "Why" line from ranking. Notes appear
  for an unranked list, for emails that could not be read, and for items left out by the
  size budget.
- **Left out:** the full "Read here" page text, the mark-read checkboxes and the other
  interactive parts of the HTML page. There are no images, scripts or external resources.
  The saved HTML stays the complete version.

### Gmail limits

- **Clipping.** Gmail clips messages larger than about 102 KB and hides the rest behind
  "View entire message". The tool keeps the HTML within a **95,000-byte** budget.
- **What is dropped to fit.** Lower-priority items go first, a whole item at a time:
  skim-first items (and unprioritised ones), then worth-reading items. **Must read items
  are never dropped.** An unranked list drops items from the end. The email then shows a
  line such as `20 of 60 items are shown; 40 lower-priority items were left out ...` that
  names the saved HTML, and the stderr line reports it.
- **Compact mode.** If dropping every droppable item is still not enough, key points and
  "Why" lines are removed from the remaining items. If that is still over budget, the
  email is sent anyway and stderr says it is over Gmail's size budget and may be clipped.
- **Long links.** A link longer than about 800 characters is shown unlinked, with a note
  to use the saved HTML.
- **Non-ASCII text** (accents, quotes, CJK) is sent as numeric character references, so
  the message is plain ASCII on the wire; it renders the same in the mail client.
- **Sending limits.** Gmail limits how many messages and recipients an account may send
  per day. See Google's page, [Gmail sending limits](https://support.google.com/mail/answer/22839),
  for the current figures; this tool asserts no number.

### Do not email the digest into your own search

If the digest is sent to the mailbox that the run reads, a broad `--query` or label can
pick the digest up on the next run. The subject always starts with `Newsletter digest:`.
Add `-subject:"Newsletter digest"` to a custom query, for example
`--query 'from:(news@example.com) -subject:"Newsletter digest"'`, or keep the label filter
narrow.

### Exit codes and stderr lines

Exit codes are in [Exit codes](#exit-codes). With `--email` the tool prints exactly one
`Email:` line to stderr:

| Outcome | stderr | Exit code |
| --- | --- | --- |
| Gmail accepted it | `Email: sent to <n> recipient(s) (Gmail id <id>).` Then, if the budget dropped items, `<shown> of <total> items included; the full list is in the saved HTML.` and, if still over budget, `The email is over Gmail's size budget and may be clipped.` | 0 |
| Nothing was sent | `Email: FAILED (<reason>). Saved: <html path>. See README > Email delivery.` | 3 |
| Ambiguous | `Email: OUTCOME UNKNOWN (<reason>); the email may have been sent, check your Gmail Sent folder before re-running. Saved: <html path>. See README > Email delivery.` | 3 |

- A bad `EMAIL_TO` or `EMAIL_SEND_TIMEOUT` is a usage error: exit 2, nothing is read from
  Gmail.
- With `--telegram --email`, both channels are attempted whatever the other one does. The
  exit code is 3 if either fails (or the email is unknown), and stderr has one line per
  channel (`Sent ... to Telegram.` or `Telegram delivery failed ...`, and `Email: ...`).
- **OUTCOME UNKNOWN** means the send was issued (or may have been) and the result is not
  known, for example the connection dropped or the server did not reply in time. The tool
  does **not** retry a send automatically, because a retry could mail every recipient
  twice. Open the **Sent** folder of the Gmail account first. Re-run only if the digest
  is not there.

### Troubleshooting email

| Symptom (in the `Email:` line) | Fix |
| --- | --- |
| `Gmail refused the send (permission or login problem ...); re-run npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` | The token is missing, expired, revoked or lacks the send permission. Re-run the `auth` command (see [OAuth scope](#oauth-scope)). |
| `Gmail rejected a recipient address (...); check EMAIL_TO` | An address in `EMAIL_TO` is not accepted by Gmail. Fix the typo or remove it. |
| `Gmail rate or sending limit reached (...); try again later` | You hit Gmail's sending limits (see [Gmail limits](#gmail-limits)). Wait and re-run. |
| `'npx' not found on PATH; install Node.js >= 18 ...` | Install Node.js 18+ or set `GMAIL_MCP_COMMAND`. |
| `could not start the Gmail MCP server after 2 attempts (...)` | Check Node.js, `GMAIL_MCP_COMMAND` and `GMAIL_MCP_ARGS`. A cold `npx` cache downloads the package on first start and can be slow; the start is tried twice (60 s each), so re-run once the download finished. |
| `the Gmail MCP server offers no send_email tool` or `the Gmail MCP server's send_email arguments differ from version 1.1.11 (...)` | `GMAIL_MCP_ARGS` does not point at the pinned `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, or another version changed the tool. Nothing was sent. See [Pinned server version](#pinned-server-version). |
| `Email: OUTCOME UNKNOWN (no reply from the Gmail MCP server within <n> s after the send was issued)` | Gmail may have accepted it. Check **Sent** before re-running. If sends are just slow, raise `EMAIL_SEND_TIMEOUT` (up to 600). |
| `Email: OUTCOME UNKNOWN (the Gmail MCP connection failed after the send was issued ...)` or `(unexpected error ...)` or `(unrecognised reply from send_email ...)` | Same: check **Sent** first, then re-run only if the digest is missing. |
| `the email failed this app's own checks (...); nothing was sent` / `could not build the email (...)` | A bug or an unexpected value in the data; the HTML is still saved. Re-run with `--log-level DEBUG` and report it. |

Error text from the server is shortened and has any address replaced before it is shown.

### Trying it end to end

This is a manual check with a real Gmail login; it is not part of the automated tests.

1. Put your own address in `.env`: `EMAIL_TO=you@example.com`.
2. Run `uv run newsletter-digest --window 1w --max-emails 5 --email`.
3. Check stdout: one line, the `.html` path. Check that stderr ends with
   `Email: sent to 1 recipient (Gmail id ...).` and that the exit code is 0 (`echo $?`).
4. In Gmail, open the message (Inbox, and a copy in Sent): the subject starts with
   `Newsletter digest:`, Must read comes first, the links open, and there is no
   "View entire message" clipping at the bottom. Check it on your phone too.
5. If the send fails with a permission error, re-run the `auth` command from
   [OAuth scope](#oauth-scope) and try again; this first send is what confirms the token.
6. Failure check: put `EMAIL_TO=not-an-address` in `.env`, re-run, and confirm exit code 2
   with a message that names the entry. Restore the correct value afterwards.

## Safety model

The tool is designed so that the AI agent can read your newsletters and nothing else.
Without `--email` no run writes to Gmail at all.

- **The agent's tools are read-only: allowlist, enforced client-side.** The MCP server
  offers 19 tools, including ones that send, modify, label and delete mail.
  `gmail_mcp.py` keeps only `search_emails` and `read_email` for the agent and discards
  the rest (default-deny). The model never sees the other tools. If an allowlisted tool
  is missing, the run stops with an error. This does not change with `--email`.
- **Sending is a separate, deterministic step (only with `--email`).** After the HTML is
  written, the app's own delivery code opens a second, short-lived Gmail MCP session and
  calls only `send_email`, with recipients from `EMAIL_TO` and a fixed, counts-only
  subject. No model is involved and no other write tool (draft, modify, delete, label,
  filter) is used by anything. See [Email delivery](#email-delivery).
- **The OAuth scope is not the guard.** The token you create in step 3 comes from the
  server's own consent flow, and its scopes include send and modify access. The allowlist
  in this tool is the only thing keeping the agent from writing. Anyone who runs the MCP
  server directly, or changes the allowlist, has that access. Treat `~/.gmail-mcp/`
  accordingly.
- **Reads are limited to the search results.** The reader can only open email ids that
  came from this run's search; any other id is refused without calling Gmail.
- **Credentials stay with the server.** Tokens and keys live in `~/.gmail-mcp/` and are
  read only by the MCP server. This tool does not read them, and the API key is excluded
  from logs and from the MCP subprocess environment.
- **The server version is pinned.** The MCP server is third-party code that holds your
  Gmail token, so the default `GMAIL_MCP_ARGS` names an exact release instead of letting
  `npx` fetch the latest one on every run. See
  [Pinned server version](#pinned-server-version).
- **Email content is untrusted data.** Each email is stripped of HTML (with lxml's
  linear-time parser, in a worker thread, so a crafted email cannot stall the run), hidden
  elements and zero-width characters, truncated, and wrapped in markers that tell the model it is
  data and not instructions. The prompts tell the model to ignore instructions found in
  emails, and the model's tools are limited to recording articles and reading listed
  emails. Delimiter look-alikes inside an email are removed so it cannot close the wrapper.
- **Prompt injection is mitigated, not eliminated.** No prompt-level defence is
  guaranteed against a determined attacker. What bounds the damage is the tool surface: a
  hostile newsletter could at worst distort the summaries, titles, links or ranking in
  your reading list. It cannot send mail, modify Gmail or reach other tools, because
  those are not available to the model (the send tool belongs to the delivery code, not
  to the agent). Treat links in the list as you would any link from an unknown
  sender.
- **The HTML output is escaped.** All dynamic content is HTML-escaped, links are limited to
  `http` and `https`, and the page has a Content-Security-Policy that blocks external
  resources and permits only its own inline script.
- **Article fetching (only with `--fetch-articles`).**
  - Only public internet addresses are contacted. Every address a name resolves to, and
    every redirect hop, is checked; loopback, private, link-local, metadata and other
    reserved ranges are refused, as are `localhost`-style and non-canonical numeric hosts.
  - Only `http` and `https` on the default ports; no credentials in the URL.
  - Caps: 15 seconds per page, 5 redirects, 2 MiB per page (compressed data is decoded
    with a bound), 20,000 characters kept (all adjustable, see
    [Environment variables](#environment-variables)).
  - No cookies or credentials are sent, and environment proxies are ignored.
  - The summariser model has no tools. Page text is treated as untrusted data, wrapped in
    markers, and the model's output and the page text are HTML-escaped in the output.
  - These checks reduce the risk of a hostile newsletter link reaching your local network;
    they do not make unknown sites trustworthy.
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
- **With `--fetch-articles`, more leaves your machine:**
  - **Publisher sites see your IP address** and a `User-Agent` of the form
    `newsletter-digest/<version> (personal reading-list tool)`.
  - **Tracking links are followed.** Newsletter links often go through the sender's click
    tracker. Following one registers a click with the sender and can reveal that you opened
    the newsletter. This is deliberate: without it many links cannot be resolved.
  - **Page text is sent to the configured OpenRouter reader model** (and on to its
    provider), as the email text already is.
  - `robots.txt` is **not** checked. The tool sends a descriptive User-Agent, caps the work
    (`--max-fetch`) and requests each URL once per run.
  - The final URLs after redirects are not written to the HTML; the title link keeps the
    original newsletter link. The extracted page text is written into the HTML ("Read
    here"), so treat the file accordingly.
  - Leave the flag off to avoid all of this.
- **With `--telegram`, the reading list leaves your machine for Telegram.** The PDF
  contains article titles, summaries, links, newsletter names and dates (the same derived
  data as the HTML, without the fetched page text), and it is uploaded to Telegram's
  servers and stored in your bot chat. The caption has counts only. Bot chats are not
  end-to-end encrypted, so Telegram can read them. The bot token and chat ID are read only
  with the flag, are not printed, and are scrubbed from log and error output. The PDF is
  also kept in the output directory. Leave the flag off to send nothing.
- **With `--email`, the reading list leaves your machine by email.** The message has
  titles, summaries, links, newsletter names, dates and the Gmail query or label (the
  same derived data as the HTML, without fetched page text beyond the summaries). It
  goes through Gmail to every address in `EMAIL_TO`; every recipient sees all the others
  in the To line. Email is not end-to-end encrypted, and a copy stays in your Sent
  folder. The subject has counts only. Recipient addresses are never logged at the
  default level and are redacted from error output (they appear only at `DEBUG`).
- The tool keeps no database or history. The only persisted state is the HTML file (plus
  the PDF with `--telegram`) and the mark-read flags in your browser. The email is kept
  by Gmail, not by this tool.

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
| `gmail_mcp.py` | MCP connection, the agent's read-only allowlist, email text preparation, and (for `--email` only) the separate `send_email` session |
| `tools.py` | Agent tools: `search_emails`, `read_email` wrapper, `record_articles`, `list_collected`, `finalize_reading_list` |
| `agent.py`, `prompts.py`, `llm.py` | Deep agent assembly, prompts, model factory |
| `models.py`, `store.py` | Pydantic models, in-memory article store and de-duplication |
| `render.py`, `templates/` | HTML reading list rendering |
| `pdf.py`, `fonts/` | PDF builder (fpdf2) and the bundled DejaVu fonts |
| `delivery.py`, `telegram.py` | `--telegram`: PDF write, size check, caption and the Telegram client (loaded only with the flag) |
| `email_delivery.py`, `email_render.py`, `templates/reading_list_email.html.j2` | `--email`: subject, send orchestration and failure mapping; Gmail-safe email HTML and plain part with the size budget; the email template (loaded only with the flag) |
| `links.py` | Link classification (article, tracker, unsubscribe, social, ...) |
| `netguard.py` | URL policy and guarded transport that refuses non-public addresses |
| `fetch.py` | `PageFetcher`: capped, classified page downloads |
| `extract.py` | Main-text extraction (trafilatura) and truncation |
| `enrich.py` | Fetch-and-summarise pass: item selection, pacing, 429 breaker |

## Limitations

- Gmail search returns one page: there is no pagination, so at most `--max-emails` (up to
  200) of the matching emails are processed.
- The Gmail MCP integration was checked against `@gongrzhe/server-gmail-autoauth-mcp`
  1.1.11, the pinned default, by reading its source and with a fake server, not against
  a live Gmail account. Other versions (set through `GMAIL_MCP_ARGS`) may change tool or
  output formats.
- Emails are truncated to 30,000 characters each; very long newsletters may lose their
  tail. Before conversion, an HTML email is also cut to 32 times that limit (about 1 MB
  of markup), which is far more markup than 30,000 characters of text need.
- Extraction, de-duplication and ranking depend on the model, so results can vary between
  runs and models.
- Testing-mode OAuth tokens expire after about 7 days (see Troubleshooting).
- Article fetching has its own limits: see
  [Limitations of fetching](#limitations-of-fetching). Summaries of fetched pages come from
  a free or cheap model and can be wrong; the full text is in "Read here".
- Mark-read state is stored per browser (keyed by article URL) and is not synced.
