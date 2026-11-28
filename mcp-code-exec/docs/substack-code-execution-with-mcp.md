# Does code execution with MCP actually save tokens?

*It cut tokens by up to 95% on big tasks*

---

Connect enough tools to an AI agent and its context window starts to look like a junk drawer. Every tool brings a written description along, and the model carries all of them on every turn, whether it touches them or not. Then there are the results. Fetch eight pages of tickets and all eight go through the model, even if the only thing it does with them is copy a few IDs into the next call.

A few terms, since the rest depends on them. **MCP** (Model Context Protocol) is a standard way for an AI app to plug into outside tools. A server publishes a menu of tools, like "get ticket" or "list projects", and each entry on the menu is described by a **schema**: the tool's name, what it does and what inputs it takes. An **agent** is a model running in a loop: it picks a tool, reads the result, and repeats until it can answer. The **context window** is everything the model can see in one request, and you pay for it in **tokens** (roughly three quarters of a word each) every time it is sent.

That last part is the whole story, so here it is plainly. A model has no memory between requests. On every turn the agent re-sends the whole conversation, including every tool result so far, and you pay for all of it again. If the model reads eight pages of tickets, those pages ride along on every later turn. Code execution avoids that: the model writes a script that reads the pages itself and sends back only the answer.

Two write-ups landed on this fix within weeks of each other. Stop describing operations as tools. Present them as code, and let the model write a script. Anthropic called it code execution with MCP. Cloudflare called it Code Mode. FastMCP has since shipped its own.

Anthropic's post leads with a 98.7% token reduction. That figure comes from a worked example, copying a meeting transcript from Google Drive into Salesforce, not from a run: no model ran, and there's no sample size. So nobody had measured it. I built a version myself, ran it against plain tool calling and against FastMCP's, and measured what happened.

## Three ways to hand an agent its tools

Everything in this post compares these three, so let me pin them down. In the tables I call them Direct, Mine and FastMCP. Mine and FastMCP are the two code-execution setups.

**Direct is plain MCP tool calling, the baseline.** The agent loads every schema into the model's context. When the model wants a tool, it doesn't run anything. It emits a structured request that names the tool and its arguments, and the harness, the agent loop around the model, executes the call and appends the result to the context. The model reads it and asks for the next one. That costs you in two ways: the schemas ride along on every turn, and every result flows through the model, even the ones it only forwards.

```
model --> tool request --> harness runs the tool --> result --> model --> tool request --> ...
```

**FastMCP's Code Mode** is a transform you put on a server. It's experimental. I ran FastMCP 4.2.6, and the docs say the feature arrived in 3.1. The model stops seeing the server's tools. It sees three: `search` to find tools, `get_schema` for a tool's parameters, and `execute`, which runs a Python script that calls the tools through `call_tool(name, params)` inside a sandbox, a locked room for running code you don't trust. Only the value the script returns goes back to the model. Everything in between stays in the script.

```
model --> search --> model --> execute( script: [ call_tool, call_tool, filter, sort ] ) --> short answer --> model
```

**Mine** is Anthropic's design, built from scratch. The harness turns each tool into a small Python file, one per tool, in a folder the model can browse. I'll call these files wrappers, since each wraps one tool in an ordinary Python function. The model lists the folder, reads only the wrappers it needs, and writes a script that imports them and calls them like normal functions. Whatever the script prints is what comes back. It's the same idea as FastMCP's with two differences: the model finds tools by reading files where FastMCP's searches, and the sandbox settings and the gateway around them are mine. Both run scripts on the same open-source interpreter.

```
model --> list and read files --> model --> run_python( script: [ tool, tool, filter, sort ] ) --> printed answer --> model
```

All three get the same task and the same system prompt, the standing instructions sent with every request. The two code setups also get an extra paragraph added to it, which I'll call the addendum, explaining how their own tool works. That paragraph turned out to matter more than I expected.

## What the difference looks like on one task

Here's task B, one of four I wrote: find the five most-commented open bug tickets, which means scanning eight pages of tickets.

With Direct, the model asks for the pages, the harness fetches them, and every page goes back through the model and stays in the context on every later turn. Eight page fetches, four model turns, 210,299 tokens sent in total.

With Mine, the model writes a short loop. The script fetches the pages inside the sandbox, checks the candidates, and prints five ticket IDs. Only those come back to the model. That's 13,725 tokens in total, even though it took eight model turns to get there.

## What I found

When the agent had to read a lot, like those eight pages or a long activity log, both code setups used 5% to 13% of the tokens Direct used. On a single lookup they used about half. Direct is fine for that.

FastMCP's Code Mode, at its defaults, cost two and a half times Direct on one task. A short paragraph added to the system prompt, explaining how its tools behave, cut that by about two thirds.

Moving my own setup from a plain Python subprocess to a stricter sandbox made it 10% to 52% more expensive in tokens. Models write Python that the new, cut-down interpreter won't run.

And how long a run takes doesn't hold still. The same untouched Direct setup took 2.0 seconds on one task one day and 6.6 the next. Ignore the timings in this post.

This is a pilot: one model, one provider, one server I wrote, four tasks I wrote, five runs of each. It tells you how the technique behaved on those tasks and nothing about yours.

---

## How the write-ups differ

In that worked example the code version "reduces the token usage from 150,000 tokens to 2,000 tokens," which is where 98.7% comes from. I wanted a number I could re-run.

The posts differ in how the model finds the tools, which matters later. Anthropic generates a file tree, one thin wrapper per tool, and the model lists the directory and reads what it needs. Cloudflare's original Code Mode puts the whole API into the context, converted to TypeScript, with one tool to execute code in a locked-down JavaScript box where the code can't see any secrets. Its later design gives the server just two tools, `search()` and `execute()`, and reports a 99.9% cut in tool-definition overhead across the entire Cloudflare API. That's a measurement of the schemas, not of a task.

## How each setup works

The target is a small server I wrote, `ticketdesk`: 31 tools over a fake dataset of 3,000 tickets, generated from a fixed seed so it's identical every run. I own the data, so I can compute the right answer without trusting the model. The model is `deepseek/deepseek-v4.1-flash-1017`, at medium reasoning effort, served by one provider that I forced so results couldn't change mid-run.

Direct is the plain loop. All 31 schemas go to the model, and every result goes through it.

For Mine, the harness reads the server's tool list at the start of each run and writes one Python wrapper per tool into a workspace. Plain string templating, no model involved:

```python
def get_latest_release(project: str) -> Any:
    """Return the most recent release of a project, given its key. ..."""
    return _mcpx_rpc.call('ticketdesk', 'get_latest_release', {'project': project})
```

The real files are longer, with a docstring per tool and typed arguments. The return type is `Any`. Outputs are untyped, so the model learns what a result looks like by printing one. The system prompt never lists the tools. This is the addendum for Mine:

```
Your tools are Python modules. You call them by writing code for the
`run_python` tool, which runs a fresh Python script in a working directory and
returns what the script printed. The script runs in a restricted Python
interpreter: only part of the standard library is available, and there is no
network or process access. In that directory, `servers/{target}/` holds one
file per tool. List the directory and read only the files you need, then import
the tools (`from servers.{target} import <tool>`) and call them as ordinary
functions. Process results in code and print only what you need. Variables do
not persist between `run_python` calls.
```

Hold on to that last sentence. Every call starts a fresh interpreter. Here's what the model did on a one-lookup task, trimmed to three of its five scripts:

```python
import os
print(os.listdir('servers/ticketdesk'))                  # 1: discover
```
```python
from servers.ticketdesk import get_latest_release
print(get_latest_release.__doc__)                        # 2: fails (see "Mine got more expensive, too")
```
```python
print(get_latest_release(project="Atlas"))               # 3: the call
```

The model never makes an MCP call itself. Its script does, through one function the sandbox hands it. On the other side a gateway in the harness (a checkpoint every call has to pass) refuses any tool that isn't this server's, refuses a script's 201st call, counts and traces everything it forwards, and passes the call on as a standard MCP request. Skip the diagram if you only want the results.

```
 tool list --> generator --> servers/ticketdesk/*.py   (mounted read-only)
                                      |
   sandbox worker: the model's script |   _mcpx_rpc.call(...)  <- the one door out
                                      v
   gateway: allowlist, 200 calls per script, count, trace --> MCP request --> ticketdesk
                                      |
                         printed output only --> model
```

Until the last benchmark the script ran in a plain Python subprocess (a separate Python process with no restrictions), which my README correctly called "not a security sandbox". Each script now gets a fresh worker, a read-only mount of the wrappers, a 60-second wall clock, 20,000 characters of output, 256 MiB, and 200 tool calls.

For FastMCP's setup I put `CodeMode()` at its defaults on a proxy that sits in front of the same server and pointed the same agent at it. The model sees three tools instead of 31:

```
search("project releases latest tag")
execute('r = await call_tool("get_latest_release", {"project": "ATLAS"})\nreturn r')
```

It finds tools by searching where mine reads files, and it runs scripts on the same interpreter, which makes it a useful comparison. A small hook on the proxy logs every real call to the server, so calls are counted the same way in all three setups.

## What I built in on purpose

A benchmark partly measures itself, so here's what mine bakes in before any numbers.

I wrote the server and the tasks. B, C and D were built to force large results or many calls, which is exactly where code execution is supposed to win. Task D's prompt even prescribes the algorithm. And I took out three conveniences a well-designed server might ship: a field listing each ticket's reviewers (without it, task C needs a lookup per ticket), a `list_deploys` tool (without it, task D has to walk the activity log), and a ranking tool for task B (without it, the model has to read the pages and rank them itself). With them in, Direct solved each task in a call or two and there was nothing left to measure.

That's a defensible choice. Real servers do have awkward APIs, and code execution papers over them. But it means the ratios answer "what does an inconvenient API cost a plain tool-calling loop?" and not "what does code execution save over a well-designed server?" I didn't run the second one.

## How I measured

I held everything the same except how the tools are offered, so any difference comes from that. All three setups get the same model, task text, system prompt, and caps of 40 model calls and 600 seconds a run, with the model's randomness set to zero. Every tool result reaches the model once. Direct and FastMCP pass results through the same callback, and the model in Mine only sees what its script prints.

The answers are checked against the known right answer, computed from the seed data, with strict checkers: an integer has to be an integer and a list has to be in order. A wrong answer isn't a saving, so the report refuses to print a ratio for any task where a single run was wrong. I ran each task on each setup five times, one run after another on one machine, taking turns between the setups so that any drift in the provider's speed hits all three alike. Every run keeps its settings and a trace of what the model did.

The task prompts name no tools, no code, no MCP:

| Task | What it forces |
|---|---|
| A: one lookup | the latest release of a project |
| B: scan 8 pages | the 5 most-commented open bug tickets |
| C: 30 lookups | 30 independent per-ticket lookups, then a tally |
| D: walk the log | the activity log from newest to oldest, to the first Atlas deploy |

## The numbers

Each figure is the median, the middle value, of five runs per task and setup: 60 runs in all, 59 correct. The numbers below are the total tokens sent to the model over the whole run, counting every turn. That's what you pay for. It isn't the size of the context window at its peak. Smaller is cheaper.

| Task | Direct | Mine | FastMCP | Mine as share of Direct | FastMCP as share of Direct |
|---|---|---|---|---|---|
| A: one lookup | 13,820 | 5,709 | 6,823 | 41% | 49% |
| B: scan 8 pages* | 210,299 | 13,725 | 17,886 | 6.5% | 8.5% |
| C: 30 lookups | 27,646 | 11,025 | 20,893 | 40% | 76% |
| D: walk the log | 149,405 | 8,051 | 19,420 | 5.4% | 13% |

\* One Direct run on B was wrong, so my report withholds the ratio. These are ratios of the medians.

On A, both code setups save about half, and Direct is the cheapest once you count the provider's cache discount, the lower price providers charge for text they've seen recently: $0.00020 a run against $0.00051 for Mine and $0.00027 for FastMCP. B and D are the big wins, with both code setups at 5% to 13% of Direct. C is thirty independent lookups. The model requests all 31 calls across three turns and the harness runs them in parallel, so Mine saves 60% and FastMCP 24%, and none of the work goes away.

The gap grows with the work for the reason from the top. Direct re-sends the whole history on every turn, so its cost grows with pages times turns. On task D it takes seven model turns, each one carrying every page read so far, and that's how you get to 149,000 tokens. The code setups keep the pages inside the script. A hand-written task-B script with no model in it (`uv run mcpx demo`) processed 445,077 characters of raw tool results inside the sandbox and printed 272 back. That shows the mechanism. It isn't a result.

Don't read Mine against FastMCP as a ranking. Their addenda were written separately and say different things, so the gap between them measures that as much as the design.

In dollars the differences are small at this scale: $0.0030 against $0.0456 a run on task D, at list price, which is the peak rate and so an upper bound. They'd start to matter with a larger model, many users, or long agent sessions, where per-run cost multiplies.

**Don't trust the timings.** The same untouched Direct setup took 2.0 seconds on task A in the batch before and 6.6 in this one. Mine took 19.6 seconds on D against 4.6 before. I timed three extra live runs of that task. Each script took under a second and the whole run 12.6 to 18.5, so the time sat between scripts, in the model's turns. I never found out why those turns were slow, and I've left timings out of the table.

## FastMCP's defaults cost two and a half times Direct

In the first benchmark with FastMCP I added nothing to the system prompt. I wanted it as shipped. On task C it used 67,741 tokens against 27,644 for Direct. I opened the traces to see why. FastMCP returns to the model only what a script `return`s, and anything it `print`s is discarded. Of the 120 scripts the model wrote, 67 printed an answer and returned nothing. The model worked this out eventually, after wasting turns: 12 model calls on C where mine took 5.

I didn't coach it, and I'm still not sure that was right, since anyone using the library would have found the same thing and fixed their prompt. For the next benchmark I gave FastMCP an addendum of the same kind mine has. It says what the three tools do, that you `await` the call, that only the returned value comes back, that variables don't persist, that the interpreter is restricted, and to process results in code. Mine carries the matching sentence, "process results in code and print only what you need." The text I use for Mine is what I'd been running before, plus one added sentence. FastMCP's was written once and edited twice before its first run. Neither was tuned against results.

| Task | FastMCP, no addendum | FastMCP, addendum | Model calls |
|---|---|---|---|
| B: scan 8 pages | 42,361 | 17,886 | 8 → 4 |
| C: 30 lookups | 67,741 | 20,893 | 12 → 6 |
| D: walk the log | 41,693 | 19,420 | 12 → 6 |

Tokens fell 58%, 69% and 53%. C went from two and a half times Direct to about three quarters of it. Scripts that printed without returning dropped from 67 of 120 to 2 of 47.

Can I say it was the addendum? Mostly. Direct didn't change and came back almost identical, 210,587 and 210,299 on B, 27,644 and 27,646 on C, 149,206 and 149,405 on D, so it wasn't the day or the provider. But I changed several things in that paragraph at once and never tested them one at a time. What I'll say is this: the default version paid a lot for a rule the tool could have stated itself. Whether that rule belongs in FastMCP's tool descriptions or in your system prompt, my data can't tell you.

## Mine got more expensive, too

After I moved it to the stricter sandbox, Mine used 10% to 52% more tokens, with one or two more model calls per run. I replayed all 106 scripts it wrote in this benchmark through the same sandbox. The traces store scripts, not outputs, and the data is fixed, so a replay is a fair stand-in. Thirty-one of them, 29%, fail, and every one of the 20 runs had at least one.

Fifteen tried `import inspect`, a standard Python module for examining code. Ten hit attribute errors on a wrapper function, mostly `__name__` and `__doc__`, or treated a function as a module. Five ended with `raise SystemExit`. One passed a wrong keyword argument, which has nothing to do with the sandbox.

Models trained on ordinary Python reach for introspection, and this cut-down interpreter says no. Most of these are cheap detours the model recovers from, but they are the extra turns. I changed the sandbox and added one sentence to the system prompt in the same step, so I can't tell you how much of the extra cost belongs to which.

## Four things that make Mine look worse

My number on task B is mostly statelessness: nothing a script computes survives to the next call. In four of five runs the model's scripts made 16 `list_tickets` calls: it fetched all eight pages, ended the script, and fetched all eight again. The likely cause is the sentence I put in the addendum. Nothing survives between calls. An interpreter that keeps its variables between calls would probably fix it, and I haven't tried one. FastMCP's setup, told the same thing, repeated the fetch in two of five runs.

The code setups also make more calls to the server, not fewer. Code execution cuts what flows through the model, not what flows through the server. On B the median was 22 calls for Mine, 14 for FastMCP and 8 for Direct, and two of FastMCP's five runs on C made 64 against a median of 33. If your server charges per call, the token numbers won't show you that.

Task A isn't a win. The code setups need extra turns to find a tool and write a script, and Direct's 31 schemas are nearly free once they're cached.

And correctness doesn't separate any of them. Fifty-nine of 60 runs were right here, and 58 of 60 in the batch before. The miss this time was a Direct run on B that answered ticket 2938 where the fifth ticket is 2935, a tie on comment count broken by id. A handful of failures in 120 runs, spread across all three, says nothing about which setup is better.

## What this doesn't show

It doesn't show the 98.7%. Schema overhead at 31 tools is modest, and these ratios aren't that number. It doesn't show that models write code better than they make tool calls, which is Cloudflare's stated reason for the approach. All three setups were nearly always correct, so what I measured was tokens and calls, mostly the effect of keeping results out of the context. It doesn't show that code beats a well-tuned Direct setup either. Mine is a plain loop. Clearing old results out of the context, searching for tools instead of loading them all, letting the model call tools from inside code, and trimming results on the server side all exist, and none of them is in the baseline.

I can't tell you which sentence of the FastMCP addendum mattered, how any of this behaves on other models or providers, or anything about GitHub's MCP server, which I haven't run. I also can't tell you the reasoning effort applied. `medium` was requested and recorded, but the counts can't say whether the provider honoured it. The framework fills missing usage fields with zero, so "0 cached tokens" can mean "not reported", and cost figures that count the cache discount depend on run order.

## Should you try this?

If your server has fewer than ten tools and small results, no. Plain tool calling is simpler, and nothing here should change your mind. If it has thirty or more tools, or tools that return big pages of data, yes, try it on one workflow, and budget time to tune the system prompt, because that's where FastMCP's defaults lost most of their tokens. If you only chat with an AI and don't build agents, you can skip this one.

One caution, because in both code setups code written by a model runs on your machine. The scripts run in a restricted interpreter with no network, no way to start other programs, and no access to your files or environment. I tested each of those for mine, and I didn't test FastMCP's, so treat even mine as probed rather than proven. The script reaches the server only through the gateway, but the gateway can still call every tool that server offers, so a tricked model can do whatever those tools allow. I ran all of this against a fake server with no secrets. Before you connect anything with real credentials, limit it to the tools the task needs and get someone else to look at the isolation.

## What's next

The setup Cloudflare's first post describes, with the whole API in the prompt and one execute tool. A run with the three removed tools put back. And the FastMCP addendum tested one sentence at a time. The code and the raw runs, with traces, are in the repo. If you find something I got wrong, tell me.

## Try it yourself

You need Python 3.13 or newer, `uv`, a clone of the repo, and an OpenRouter API key in a `.env` file in `mcp-code-exec` (the README has the steps). All 60 runs cost about a dollar at list price and take 25 to 30 minutes. I ran them one task at a time, which also means a crash costs you one task, not all four:

```
for t in A B C D; do
  uv run mcpx run --target ticketdesk --arm all --task $t --runs 5 \
    --model deepseek/deepseek-v4.1-flash-1017 --provider parasail --out runs/ticketdesk-v5-$t.jsonl
done
uv run mcpx report runs/ticketdesk-v5-*.jsonl
```

Expect the same pattern, not the same numbers. Model output varies from run to run and providers change underneath you. My own runs are committed with their traces in `docs/results/ticketdesk-v5-2026-11-26.jsonl`, and every record carries a settings string, so you can check that yours ran the same configuration before you compare.

---

## Credits and sources

- **Code execution with MCP**: Adam Jones and Conor Kelly, Anthropic Engineering, November 4, 2025. The file-tree-of-tools and progressive-disclosure design, the `search_tools` alternative, and the 150,000 → 2,000 token illustration. https://www.anthropic.com/research/code-execution-with-mcp
- **Code Mode: the better way to use MCP**: Kenton Varda and Sunil Pai, Cloudflare, September 26, 2025. MCP tools as a typed API, V8-isolate execution, bindings so the code never sees a key. https://blog.cloudflare.com/code-mode/
- **Code Mode: give agents an entire API in 1,000 tokens**: Cloudflare (the page lists Matt Carey), February 20, 2026. The two-tool `search()` and `execute()` design. https://blog.cloudflare.com/code-mode-mcp/
- **FastMCP Code Mode**: FastMCP documentation (experimental, new in 3.1.0). The `search`, `get_schema` and `execute` meta-tools. https://gofastmcp.com/docs/servers/transforms/code-mode

Built with Google ADK for the agent loop, LiteLLM and OpenRouter for the model calls, the official MCP Python SDK and FastMCP. Benchmarked on `deepseek/deepseek-v4.1-flash-1017` served by Parasail. Code, plan, spec and raw results: `agent-foundry/mcp-code-exec`.

---

*The thinking here is mine, worked out through research and conversations; the phrasing and structuring were curated with the help of an LLM.*
