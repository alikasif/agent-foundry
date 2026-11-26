# I tested "code execution with MCP" on a real model. Here's what held up.

*Three ways to give an agent 31 tools, 60 runs in total, and a result I didn't expect.*

---

Two write-ups proposed the same fix for agents with too many tools within weeks of each other: present the tools as code, and let the model write a script that calls them. Anthropic called it code execution with MCP. Cloudflare called it Code Mode. FastMCP has since shipped its own version. I built my own, ran it against plain tool calling and against FastMCP's, and measured. This is what came out.

## Three setups, in plain terms

Everything below compares three ways of giving an agent its tools, so here they are first.

**1. Plain MCP tool calling (the baseline).** An MCP server describes each tool with a schema. The agent loads every schema into the model's context, and when the model wants a tool it emits a structured request naming the tool and its arguments. The model never runs anything itself: the harness (the agent loop around the model) executes the call against the MCP server and appends the result to the context. The model reads it and decides on the next request. That has two costs. The schemas ride along on every turn, used or not. And every result passes through the model, even when all the model does is copy it into the next call. With dozens of tools and paginated results, both add up. I call this setup "direct".

```
model --> tool request --> harness runs the tool --> result --> model --> tool request --> ...
```

**2. FastMCP's Code Mode.** FastMCP is a widely used library for building MCP servers, and its Code Mode (experimental; I ran FastMCP 4.2.6, and the docs say it arrived in 3.1) is a transform you put on a server. The model no longer sees the server's tools. It sees three: `search` (find tools), `get_schema` (a tool's parameters) and `execute`, which runs a Python script that calls the tools with `call_tool(name, params)` inside a sandbox. Only the value the script returns goes back to the model. The intermediate work stays in the script.

```
model --> search --> model --> execute( script: [ call_tool, call_tool, filter, sort ] ) --> short answer --> model
```

**3. My setup.** This is Anthropic's "code execution with MCP" design, built from scratch. The harness turns each tool into a small Python file, one per tool, in a folder the model can browse. The model lists the folder, reads only the files it needs, then writes a script that imports those wrappers and calls them like ordinary functions. What the script prints is what comes back. The idea matches FastMCP's, with two differences: the model finds tools by reading files where FastMCP's searches, and the sandbox that runs the scripts is mine.

```
model --> list and read files --> model --> run_python( script: [ tool, tool, filter, sort ] ) --> printed answer --> model
```

The model gets the same task and the same base instruction in all three. The two code setups also get a short paragraph in the prompt explaining their own tool, which turns out to matter more than I expected.

## What I found

- **The saving is real and depends on the task.** When a task makes the model read a lot (scanning pages, walking a log), both code setups used 5% to 13% of the tokens direct tool calling used. On a single lookup they used about half. Direct calling is fine for that.
- **FastMCP's Code Mode, at its defaults, cost 2.5 times direct on one task.** A short paragraph in the system prompt, explaining how its tools behave, cut that by roughly two thirds.
- **A stricter sandbox costs turns.** Moving my own setup from a plain Python subprocess to a sandboxed interpreter that isn't CPython made it 10% to 52% more expensive in tokens, because models write Python the interpreter won't run.
- **Timings don't hold still.** The unchanged direct setup took 2.0 seconds on a task one day and 6.6 the next. Ignore wall time here.

**Read this first.** This is a pilot: one model, one provider, one server I wrote, four tasks I wrote, five runs per cell. It shows how the technique behaves on those tasks and says nothing about yours. The limits are collected at the end, and I won't repeat them in every section.

If your server has fewer than ten tools and small results, ignore all of this. If it has thirty or more tools, or returns large paginated results, it is worth trying, and worth budgeting for the prompt work it needs.

---

## Where the 98.7% comes from

Anthropic's post carries the headline number: a 98.7% token reduction. That figure is an illustration. It belongs to a worked example (copy a meeting transcript from Google Drive into Salesforce), where the code version "reduces the token usage from 150,000 tokens to 2,000 tokens." No model ran, and there is no sample size. So nobody had measured it, and I measured something.

The posts share the core move described above and differ in how the model finds the tools, which matters later:

- **Anthropic** generates a file tree of the tools, one thin wrapper per file, and the model lists the directory and reads only the files it needs.
- **Cloudflare's original Code Mode** puts the whole API, converted to TypeScript, in the context, with one tool to execute code in a V8 isolate. The code never holds an API key.
- **Cloudflare's later design** gives the server two tools, `search()` and `execute()`. It reports a 99.9% cut in tool-definition overhead for the entire Cloudflare API, which is a measurement of the schema, not of a task.

I wanted one end-to-end measurement of the idea that I could re-run: same task, same model, same base instruction, three setups, real LLM calls only. A scripted model can only measure itself.

## How the setups work

The stack is Python, Google ADK for the agent loop, LiteLLM and OpenRouter for the model (`deepseek/deepseek-v4.1-flash-1017`, medium reasoning effort, provider pinned, fallbacks off), and the official `mcp` SDK. The target is a small local server I wrote, `ticketdesk`: 31 tools over a seeded fake dataset of 3,000 tickets. I own the data, so I can compute the right answer without trusting the model.

**Direct** is the plain loop: all 31 schemas through ADK's `McpToolset`, every result through the model.

**My code setup.** At the start of each run the harness reads `tools/list` and writes one Python wrapper per tool into a workspace. Plain string templating, no model involved:

```python
def get_latest_release(project: str) -> Any:
    """Return the most recent release of a project, given its key. ..."""
    return _mcpx_rpc.call('ticketdesk', 'get_latest_release', {'project': project})
```

The real files are longer (a docstring per tool, typed arguments). The return type is `Any`: outputs are untyped, so the model learns what a result looks like by printing one. The prompt doesn't list the tools. The code setup adds this to the shared base instruction:

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

The last sentence matters later: every call starts a fresh interpreter. Here is what the model did on the one-lookup task, abridged to three of its five scripts:

```python
import os
print(os.listdir('servers/ticketdesk'))                  # 1: discover
```
```python
from servers.ticketdesk import get_latest_release
print(get_latest_release.__doc__)                        # 2: fails in the sandbox
```
```python
print(get_latest_release(project="Atlas"))               # 3: the call
```

The model never makes an MCP call itself. Its script does, through one function the sandbox is handed (`_mcpx_rpc.call`). On the other side a gateway in the harness refuses any tool that isn't this server's, refuses a script's 201st call, counts and traces every call it forwards, and passes it on over a standard MCP `tools/call`.

```
 tools/list --> generator --> servers/ticketdesk/*.py   (mounted read-only)
                                      |
   sandbox worker: the model's script |   _mcpx_rpc.call(...)  <- the one door out
                                      v
   gateway: allowlist, 200 calls per script, count, trace --> MCP tools/call --> ticketdesk
                                      |
                         printed output only --> model
```

Until the last benchmark the script ran in a plain `python -S` subprocess, which my README correctly called "not a security sandbox". Each script now gets a fresh worker, a read-only mount of the wrappers, a 60-second wall clock, 20,000 characters of output, 256 MiB, and 200 tool calls.

**FastMCP's setup.** I put `CodeMode()` at its defaults on a stdio proxy of the same server and pointed the same agent at the proxy. The model sees three tools instead of 31:

```
search("project releases latest tag")
execute('r = await call_tool("get_latest_release", {"project": "ATLAS"})\nreturn r')
```

Discovery by search instead of by reading files, and the same kind of sandbox underneath, which makes it a useful comparison. A middleware on the proxy logs every real backend call, so tool calls are counted the same way in all three setups.

## What I designed in

A benchmark partly measures itself, so here is what mine bakes in, before the numbers.

I wrote the server and the tasks. B, C and D were built to force large results or many calls, which is where code execution is supposed to win. Task D's prompt even prescribes the algorithm. And I removed three conveniences a well-designed server might ship, a field listing each ticket's reviewers, a `list_deploys` tool and a ranking tool for task B, because with them the direct setup solved the task in a call or two and there was nothing to measure.

That's a defensible choice, since real servers do have awkward APIs and code execution papers over them. But the ratios answer "what does an inconvenient API cost a plain tool-calling loop?", not "what does code execution save over a well-designed server?". The second question I did not run.

## How I measured

- Same model, provider, temperature (0), task text, base instruction and caps (40 model calls, 600 seconds) in all three setups.
- The clock excludes start-up work: server start, tool discovery, workspace and sandbox start-up. Each script's own worker spawn is inside it, 1 to 10 milliseconds.
- Each result reaches the model once. The direct and FastMCP setups pass results through the same callback, and my setup's model only sees what its script prints.
- Ground truth comes from the seed data, with strict checkers: an integer must be an integer, a list must be in order.
- A wrong answer isn't a saving. The report refuses to print a ratio for a task where any run was wrong.
- Five runs per cell, setups interleaved by task and run, sequential on one machine. Every record stores its settings and a trace of what the model did.

The prompts name no tools, no code, no MCP:

| Task | What it forces |
|---|---|
| A | one lookup: the latest release of a project |
| B | scan 8 pages of open bug tickets for the 5 most-commented |
| C | 30 independent per-ticket lookups, then a tally |
| D | walk the activity log from newest to oldest to the first Atlas deploy |

To reproduce it (the four pieces concatenate to the committed results file, and every record carries its settings string, so you can diff yours against mine):

```
for t in A B C D; do
  uv run mcpx run --target ticketdesk --arm all --task $t --runs 5 \
    --model deepseek/deepseek-v4.1-flash-1017 --provider parasail --out runs/ticketdesk-v5-$t.jsonl
done
uv run mcpx report runs/ticketdesk-v5-*.jsonl
```

## Results

Medians of five runs per cell, 60 runs, 59 correct. Tokens are cumulative prompt tokens across all model calls in a run, which is what you pay for, not the size of the context window at its peak.

| Task | Direct | Code (mine) | FastMCP | Code as share of direct | FastMCP as share of direct |
|---|---|---|---|---|---|
| A | 13,820 | 5,709 | 6,823 | 41% | 49% |
| B* | 210,299 | 13,725 | 17,886 | 6.5% | 8.5% |
| C | 27,646 | 11,025 | 20,893 | 40% | 76% |
| D | 149,405 | 8,051 | 19,420 | 5.4% | 13% |

\* One direct run on B was wrong, so my report withholds the ratio. These are ratios of the medians.

- **A:** both code setups save about half. Direct is the cheapest option once the provider's cache discount is counted ($0.00020 a run, against $0.00051 for mine and $0.00027 for FastMCP's).
- **B and D:** the big wins. Both code setups stay at 5% to 13% of direct.
- **C:** thirty independent lookups. In the direct setup the model requests all 31 calls across three turns and the harness runs them in parallel, so mine saves 60% and FastMCP's 24%, and none of the work disappears.

The mechanism is plain. Direct re-sends the whole history on every turn, so its cost grows with pages times turns. On task D direct takes seven model turns, each carrying every page read so far, which is how 149,000 tokens arise. The code setups keep the pages inside the script. A hand-written task-B script with no model in it (`uv run mcpx demo`) processed 445,077 characters of raw tool results inside the sandbox and printed 272 back. That shows the mechanism, not a result.

In dollars the differences are small at this scale: $0.0030 against $0.0456 per run on task D, at list price (the peak rate, so an upper bound). They would start to matter with a larger model, many users or long agent sessions, where per-run cost multiplies.

**On timings, don't trust them.** The same unchanged direct setup took 2.0 seconds on task A in the previous batch and 6.6 in this one. My code setup took 19.6 seconds on D against 4.6 before. I timed three extra live runs of that task: each script took under a second and the runs 12.6 to 18.5, so the time sat between scripts, in the model's turns. I never found out why those turns were slow, and I have left wall time out of the table.

## The surprise: FastMCP at its defaults

In the first benchmark with FastMCP I added nothing to the prompt. I wanted FastMCP as shipped. On task C it used 67,741 prompt tokens, against 27,644 for direct: two and a half times more. I opened the traces and found the reason. FastMCP returns to the model only what a script `return`s, and `print()` output is discarded. Of 120 scripts the model wrote, 67 printed an answer and returned nothing. The model figured this out eventually, after wasting turns. It took 12 model calls on C where my setup took 5.

I didn't coach it, and that was a decision I'm still not sure about, since a user of the library would have found the same thing and fixed their prompt. For the next benchmark I gave FastMCP a short addendum of the same kind my setup has: what its three tools do, the `await`, that only the returned value comes back, that variables don't persist, the restricted interpreter, and one sentence telling it to process results in code. Mine carries the matching sentence ("process results in code and print only what you need"). My addendum has been in place since the third benchmark with one sentence added for this one; FastMCP's was written once and edited twice before its first run, and neither was tuned against results.

| Task | FastMCP, no addendum | FastMCP, addendum | Model calls |
|---|---|---|---|
| B | 42,361 | 17,886 | 8 → 4 |
| C | 67,741 | 20,893 | 12 → 6 |
| D | 41,693 | 19,420 | 12 → 6 |

Prompt tokens fell 58%, 69% and 53%, and C went from 2.5 times direct to about three quarters of it. Scripts that printed without returning dropped from 67 of 120 to 2 of 47.

Why believe it was the addendum? The direct setup didn't change and came back almost identical (B 210,587 and 210,299, C 27,644 and 27,646, D 149,206 and 149,405), so it isn't the day or the provider. Why not believe it fully? I changed several things in that addendum at once and didn't test them one at a time. A fair reading: the default version paid heavily for a rule the tool could have stated itself, and whether that belongs in FastMCP's tool descriptions or your system prompt is a question this data doesn't answer.

## The second surprise: Python the sandbox won't run

My own setup got more expensive after I moved it to the stricter sandbox, by 10% to 52% in prompt tokens, with one or two more model calls per run. I replayed all 106 scripts it wrote in this benchmark through the same sandbox (the traces store scripts, not outputs, and the data is fixed, so a replay is a fair stand-in). 31 of them, 29%, fail, and every one of the 20 runs had at least one failure:

- `import inspect`: 15
- attribute errors on a wrapper function (`__name__`, `__doc__`, treating a function as a module): 10
- `raise SystemExit`: 5
- a wrong keyword argument, which has nothing to do with the sandbox: 1

Models trained on CPython reach for introspection, and this interpreter rejects it. Most failures are cheap detours the model recovers from, but they are the extra turns. I changed the sandbox and added one sentence to the prompt in the same step, so I can't say how much of the extra cost is which.

## Four results that make my setup look worse

- **My B number is mostly statelessness.** In four of five task-B runs the model's scripts made 16 `list_tickets` calls: it fetched all eight pages, ended its script, and fetched all eight again. The likely cause is the sentence I put in the prompt, since nothing survives between calls. A persistent interpreter would probably cut it; I haven't tried. FastMCP's setup, told the same thing, repeated the fetch in two of five runs.
- **The code setups make more MCP calls, not fewer.** Code execution cuts what flows through the model, not what flows through the server. On B the median was 22 calls for mine, 14 for FastMCP and 8 for direct, and two of FastMCP's five runs on C made 64 against a median of 33. If your server charges per call, the token column won't show you that.
- **Task A is not a win.** The code setups need extra turns to find a tool and write a script, and direct's 31 schemas are nearly free once cached.
- **Correctness doesn't separate the setups.** 59 of 60 runs were right here and 58 of 60 in the batch before. The miss here was a direct run on B that answered ticket 2938 where the fifth ticket is 2935 (a tie on comment count broken by id). A handful of failures in 120 runs, spread across setups, says nothing about which setup is better.

## What this shows

On one model, one provider, one dataset and four tasks:

- Presenting tools as code the model writes cut cumulative prompt tokens to between 5% and 76% of direct calling, with the gap growing with the size of the results.
- A code-execution tool is not a drop-in. FastMCP's cost 2.5 times direct on one task until the prompt told the model how it behaves, and my setup paid more for a stricter sandbox.
- Direct tool calling is the right answer for tiny tasks.

What it doesn't show:

- **The 98.7%.** Schema overhead at 31 tools is modest, and these ratios aren't that number.
- **That models write code better than they make tool calls.** That is Cloudflare's stated reason. All three setups were nearly always correct, so I measured tokens and calls, mostly the effect of keeping results out of the context.
- **That code beats a well-tuned direct setup.** Mine is a plain loop. Result clearing, tool search, programmatic tool calling and server-side projection all exist, and none is in the baseline.
- **Which sentence of the FastMCP addendum mattered,** other models or providers, or anything about GitHub's MCP server, which I haven't run.
- **That the reasoning effort applied.** `medium` was requested and recorded, but the counts can't say whether the provider honoured it. ADK also fills missing usage fields with zero, so "0 cached tokens" can mean "not reported", and cache-adjusted costs depend on run order.

## What's next

The setup Cloudflare's first post describes (the whole API in the prompt plus one execute tool), a run with the three removed tools put back, and the FastMCP addendum tested one sentence at a time. The code and the raw runs, with traces, are in the repo. If you find something I got wrong, tell me.

---

## Credits and sources

- **Code execution with MCP**: Adam Jones and Conor Kelly, Anthropic Engineering, November 4, 2025. The file-tree-of-tools and progressive-disclosure design, the `search_tools` alternative, and the 150,000 → 2,000 token illustration. https://www.anthropic.com/research/code-execution-with-mcp
- **Code Mode: the better way to use MCP**: Kenton Varda and Sunil Pai, Cloudflare, September 26, 2025. MCP tools as a typed API, V8-isolate execution, bindings so the code never sees a key. https://blog.cloudflare.com/code-mode/
- **Code Mode: give agents an entire API in 1,000 tokens**: Cloudflare (the page lists Matt Carey), February 20, 2026. The two-tool `search()` and `execute()` design. https://blog.cloudflare.com/code-mode-mcp/
- **FastMCP Code Mode**: FastMCP documentation (experimental, new in 3.1.0). The `search`, `get_schema` and `execute` meta-tools. https://gofastmcp.com/docs/servers/transforms/code-mode

Built with Google ADK, LiteLLM, OpenRouter, the official MCP Python SDK and FastMCP. Benchmarked on `deepseek/deepseek-v4.1-flash-1017` served by Parasail. Code, plan, spec and raw results: `agent-foundry/mcp-code-exec`.

---

*The thinking here is mine, worked out through research and conversations; the phrasing and structuring were curated with the help of an LLM.*
