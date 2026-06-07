# CodeAct: Why the Best Action Space for an LLM Agent Is Code

*Part 1 of a series. We build a CodeAct agent from the ground up, compare it head-to-head with the ReAct pattern, and find the wall it hits — the one Recursive Language Models are built to climb. If you've ever wired up a single tool-calling agent, you're the reader this is written for.*

---

## We came a long way to get here

Not long ago, "using an LLM" meant one thing: send a prompt, read the completion. Then we asked models to *do* things, and the stack grew fast. We bolted on **tools and function calling** so a model could fetch a real fact or hit an API instead of inventing one. We wrapped those calls in loops and got **agents** that plan, act, and adjust. We standardized the plumbing with protocols like **MCP**, so any model could reach any tool without bespoke glue for each one. Every step pushed capability outward — from "predict text" to "operate software."

But one constraint never moved: the **context window**. Everything a model can use in the moment must fit inside it, and every tool result, every retrieved document, every observation competes for that finite space. Look closely and most of the recent invention in agent design — RAG, function calling, MCP, multi-agent systems, and the pattern this article is about — is really a set of strategies for doing *more* without drowning the window. The newest of these patterns changes something more fundamental than which tools an agent owns. It changes **how the agent expresses an action in the first place.** That is CodeAct.

---

## Start with a task that breaks the usual agent

Give an agent this job:

> "Read this 5,000-row sales export, find the three most expensive orders in each region, and write a one-paragraph summary."

Watch how a conventional tool-calling agent handles it. It calls `read_file`, and the entire export comes back as a wall of text dumped straight into its context window. Now it has to produce the top three per region by *reasoning over raw rows in prose* — scanning, comparing, and tallying in natural language, token by token. With four regions and thousands of rows, it drifts. It miscounts. It is performing a `GROUP BY` from memory, which is the one operation a language model should never be handed.

Now watch a CodeAct agent. It writes this:

```python
import pandas as pd
df = pd.read_csv("sales.csv")
top = (df.sort_values("total", ascending=False)
         .groupby("region")
         .head(3))
print(top.to_string())
```

One action. The grouping, sorting, and slicing happen inside a real Python runtime that already knows how to do them correctly. The agent never has to *remember* the 5,000 rows — it operates on them.

That difference is not cosmetic. It is the difference between an agent whose **action space** is a fixed menu of hand-written tools and one whose action space is a Turing-complete programming language. This article is about that idea — where it came from, why it measurably wins, and exactly how we built a working CodeAct agent that runs Python, Bash, and Awk in a persistent REPL.

---

## An agent is only as good as its action space

Strip away the branding and every LLM agent is the same loop: the model looks at a state, *emits an action*, the environment runs the action and returns an *observation*, and the loop repeats until the model decides it's done. The model's intelligence sets a ceiling on the loop, but so does something more mundane — **the format the action has to be expressed in.**

If the only actions you can express are "call one of these twelve functions with these arguments," then no matter how clever the model is, it cannot say "loop over these items, and for each one that matches, fetch its detail and accumulate a running total." It has to *simulate* that loop by hand, one function call and one round-trip at a time, holding the intermediate state in its context. Every round-trip is a chance to drift, and every intermediate result is more tokens piling up in the window.

The action space *is* the bottleneck. The history of agent design is largely a history of widening it.

---

## ReAct: reason, act, observe

The pattern that made agents practical is **ReAct** (Yao et al., 2022): *Reasoning + Acting*. Its insight was that a language model does better when it interleaves a thought with each action instead of acting blind. A ReAct trajectory looks like this:

```
Thought: I need the population of France's capital.
Action: search("capital of France")
Observation: Paris
Thought: Now I need Paris's population.
Action: search("population of Paris")
Observation: 2.1 million
Thought: I have the answer.
Action: finish("About 2.1 million")
```

This is genuinely good design, and it's worth being fair about *why*. The reasoning trace is interpretable — you can read exactly why the agent did what it did. It's simple to implement: the model emits text in a `Thought/Action/Observation` shape, a harness parses the `Action` line, runs the matching tool, and pastes the result back as the next `Observation`. It grounds the model's reasoning in real feedback instead of letting it hallucinate a whole plan up front.

But look at the shape of the constraint. Each step emits **exactly one action**, chosen from a **fixed set of tools**, expressed as **text the harness has to parse**. Three consequences follow:

- **No composition.** You cannot combine two tools in one step. "Search, then for each result fetch the page, then sum the numbers" is not one action — it's a sequence the model must drive turn by turn.
- **No control flow.** There is no native loop, no conditional, no variable. If the task needs "repeat until," the model performs the repetition manually, re-deciding each iteration.
- **Glue logic lives in prose.** The actual work *between* tool calls — filtering, counting, reshaping — happens in the model's head and is emitted as natural language, which is exactly where models are weakest and most likely to fabricate.

You feel all three the moment a task needs to *combine* tools. Ask a ReAct agent to total the prices of every in-stock product, and it has no loop to reach for — so it *becomes* the loop: list the items, then check stock and update a running total one product, one turn, at a time. Two hundred products turn into hundreds of round-trips. The running total lives in the model's reasoning text, where a single slip silently corrupts the answer. ReAct widened the action space from "answer in one shot" to "act, observe, and adapt." It did not widen it to "do arbitrary computation." That next step needed a different substrate.

---

## The JSON detour: same ceiling, sturdier walls

The mainstream industrialized ReAct into **structured tool calling** — the JSON function-calling APIs every provider now ships. Instead of emitting a fuzzy `Action:` line for a harness to parse, the model emits a strict JSON object: `{"name": "search", "arguments": {"query": "..."}}`. Models are fine-tuned to produce this reliably, the arguments are schema-validated, and the parsing ambiguity of free-text actions disappears.

This is a real improvement in *reliability*. It is not an improvement in *expressiveness*. A schema is still a fixed menu. You still get one call per step (or a flat batch of independent calls with no data flowing between them). You still cannot express a loop, a conditional, or "take the output of call A, transform it, and feed it into call B" without bouncing every intermediate value back through the model's context. Newer APIs added *parallel* tool calls, but parallel is not the same as *composed*: the calls still can't read each other's results, so anything with a dependency between steps collapses back into sequential round-trips. The `langgraph-codeact` project frames the trade-off bluntly — code actions enable "solving more complex tasks in less steps" precisely because JSON function-calling can't compose or branch on its own. There's a tell in its design, too: the moment you let an agent act in code, *persistent variables* become the thing you have to manage. Hold that thought. It's the center of gravity for everything that follows.

JSON tool-calling built sturdier walls around the same room. To leave the room you have to change what an action *is*.

---

## CodeAct: code *is* the action space

The 2024 paper **"Executable Code Actions Elicit Better LLM Agents"** (Wang et al., arXiv 2402.01030) made the move explicit: stop treating code as one tool among many, and make **executable code the unified action space itself.** Every action the agent takes is a snippet of Python, run in a live interpreter, whose output (results *and* errors) comes back as the observation.

The moment you do this, the three ReAct constraints dissolve:

- **Composition is free.** A single action can call three functions, transform the results, and combine them — because it's just code.
- **Control flow is free.** `for`, `if`, `while`, comprehensions, exception handling — all native, all in one action.
- **The entire software ecosystem becomes your toolset.** You no longer hand-write a `read_csv` tool and a `groupby` tool and a `top_n` tool. You `import pandas`. Any library on PyPI is an action the agent already knows how to use, because models are pretrained on enormous amounts of real code.

The paper's authors lay out four advantages, and each one is worth making concrete:

- **Existing software packages.** The "out of stock total" task above is `sum(p.price for p in products if p.in_stock)` — one line, using `sum` and a generator the model already knows. Nobody had to design, document, and register a `total_in_stock_prices` tool. The standard library and PyPI are a pre-built toolbox of millions of functions.
- **Control and data flow.** Loops, conditionals, `try/except`, and — critically — **variables**. Data produced by one part of an action flows into the next without a round-trip. The model orchestrates; it doesn't shuttle every intermediate value back through its own context.
- **Pretraining familiarity.** Models have read staggering amounts of real code. Asking them to express intent as Python is asking them to speak a language they're already fluent in, rather than a bespoke JSON dialect invented for one API.
- **Efficiency.** Because one action can do the work of many tool calls, tasks finish in fewer turns — which is both cheaper and less error-prone, since every avoided turn is an avoided chance to drift.

And the numbers back it. On **M3ToolEval**, a benchmark of complex tool-composition tasks, the strongest model tested hit **74.4%** success with code actions versus **53.7%** with text — what the paper calls a "20.7% absolute improvement compared to the next best action format." Fewer turns, fewer actions, higher success: efficiency and accuracy move together. The rest of the evidence is consistent.

| Claim (CodeAct paper, arXiv 2402.01030) | Figure |
|---|---|
| CodeAct vs text on M3ToolEval (gpt-4-1106-preview) | 74.4% vs 53.7% (+20.7 pts) |
| Interaction turns saved, on average | 2.1 fewer |
| LLMs where CodeAct has the higher success rate | 12 of 17 |
| Action reduction on complex tasks | up to 30% fewer |
| API-Bank, Llama-2-13b-chat: code vs JSON actions | 38.1% vs 8.5% |

That last row is the quiet one: for a model never drilled on JSON formatting, expressing actions as code it already understands is nearly five times more reliable. And this is a trainable skill, not a prompt trick — to make smaller open models fluent in it, the authors released **CodeActInstruct** (7,139 multi-turn interactions) and **CodeActAgent**, finetuned from Llama 2 and Mistral, reaching 51.3% and 57.4% on in-domain MINT tasks without regressing on general benchmarks.

---

## How it actually works — our implementation

Theory is cheap. Here is the real loop, from a working CodeAct agent we built (`codeact_agent/`). The whole thing is a four-beat cycle: **generate → execute → observe → self-debug**. Let's walk the real code. *(The snippets below are lightly trimmed for readability — logging, some docstrings, and defensive branches removed — but otherwise match the repository.)*

### Beat 1: extract code from the model's reply

The model replies in markdown. We pull out the fenced code blocks and normalize the language tag:

```python
_CODE_BLOCK_RE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)

def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for match in _CODE_BLOCK_RE.findall(text):
        raw_lang = (match[0] or "text").lower().strip()
        lang = _LANG_ALIASES.get(raw_lang, raw_lang)  # "py" -> "python", "sh" -> "bash"
        code = match[1].strip()
        blocks.append((lang, code))
    return blocks
```

No special action grammar, no JSON schema. The action *is* the code block.

### Beat 2: execute in a persistent REPL

This is the detail that separates a real CodeAct agent from a toy. The naive implementation runs each block in a throwaway namespace:

```python
exec(code, {})   # WRONG: every block starts from nothing
```

With a fresh namespace each time, a variable defined in one turn is gone by the next. The agent can't build up state, can't define a helper and reuse it, can't load data once and query it repeatedly. So our sandbox keeps **one namespace for the whole session**:

```python
class PythonSandbox:
    def __init__(self, child_runner=None, workspace=None):
        self._namespace: dict[str, object] = {}
        # ...
        self._inject_builtins()      # FINAL, context, llm_query live here

    async def execute(self, code: str, timeout=None) -> SandboxResult:
        self._inject_builtins()       # keep specials fresh even after reset
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        exc_str = None
        final_val = None
        try:
            exec(code, self._namespace)        # SAME namespace, every call
        except FinalSignal as fs:
            final_val = fs.value               # FINAL() was called — not an error
        except Exception:
            exc_str = traceback.format_exc()   # capture the traceback for self-debug
        finally:
            stdout_val = sys.stdout.getvalue()
            stderr_val = sys.stderr.getvalue()
            sys.stdout, sys.stderr = old_stdout, old_stderr
        return SandboxResult(stdout=stdout_val, stderr=stderr_val,
                             exception=exc_str, final_value=final_val)
```

Because `self._namespace` persists, the agent's runtime behaves like a Jupyter session: `df = pd.read_csv(...)` in turn one is still there in turn five. State accumulates instead of evaporating.

### Beat 3: turn the result into an observation

Whatever the code printed, errored, or raised gets wrapped in a compact, machine-readable observation and handed back to the model. Crucially, we **truncate** stdout and stderr first — each stream is capped independently, so a single oversized `print` can't flood the window:

```python
def truncate_output(text: str, max_chars: int = 4_000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    marker = f"\n… [{omitted} chars omitted] …\n"
    return text[:half] + marker + text[-half:]   # keep head AND tail

def _format_observation(result, max_chars=4_000) -> str:
    parts = [
        "<execution>",
        "<stdout>", truncate_output(result.stdout, max_chars) if result.stdout else "", "</stdout>",
        "<stderr>", truncate_output(result.stderr, max_chars) if result.stderr else "", "</stderr>",
        "<exception>", result.exception if result.exception else "None", "</exception>",
        "</execution>",
    ]
    return "\n".join(parts)
```

### Beat 4: loop until done — and self-debug along the way

The orchestration ties it together. The agent runs, we execute its blocks, we feed back observations, and we repeat. If a block raised, the traceback *is* the observation — so the model sees its own error and fixes it on the next turn, with no human in the loop:

```python
for turn_idx in range(self._max_turns):
    response = await self._agent.run(current_history)
    code_blocks = extract_code_blocks(response)

    if not code_blocks:
        return response, current_history          # no code -> final answer

    for (lang, code) in code_blocks:
        result = await self._registry.dispatch(lang, code)

        if result.final_value is not None:        # FINAL(answer) was called
            return str(result.final_value), current_history

        observation = _format_observation(result, self._output_max_chars)
        current_history.append({"role": "user", "content": observation})
```

A real self-debug trace falls straight out of this. The model writes `import csv; rows = csv.reader(open("data.tsv"))` and forgets the delimiter; the observation carries a `csv.Error`; the next turn it writes `csv.reader(open("data.tsv"), delimiter="\t")` and moves on. The error message *is* the feedback signal. This is the same generate→execute→observe→self-debug loop the CodeAct paper describes, and that practitioners rediscover every time they build one — capture stdout/stderr, feed back the traceback, let the model correct itself.

### `FINAL()`: a clean exit

How does the loop know the agent is *done* rather than just chatty? We give the agent an explicit termination builtin. Calling `FINAL(answer)` raises a sentinel that the sandbox catches and the loop treats as the end of the turn:

```python
class FinalSignal(BaseException):       # BaseException, not Exception —
    def __init__(self, value):          # so a stray `except Exception` in
        super().__init__(value)         # agent code can't swallow it
        self.value = value
```

Inheriting from `BaseException` is deliberate: agent code is full of `try/except Exception` blocks, and the termination signal must punch through all of them. The answer can be a string *or* any Python object, which matters more than it looks — it means the final result isn't bound by what the model can retype token-by-token.

### Watching it think

A loop like this can sit silent for ten seconds while the model produces its first token, and silence reads as *frozen*. Modern models also emit a **reasoning stream** before the answer text, surfaced by the SDK as a separate event type. We render both as they arrive, so you can watch the agent reason before it acts:

```python
async for event in stream_result.stream_events():
    if isinstance(event.data, ResponseReasoningTextDeltaEvent):
        on_reasoning_token(event.data.delta)   # dim, "thinking…" trace
    elif isinstance(event.data, ResponseTextDeltaEvent):
        on_stream_token(event.data.delta)       # the actual answer
```

The reasoning is display-only — never fed back into code extraction — so the agent's private deliberation can't pollute the action it takes. A small craft detail, but it's the difference between an agent that feels alive and one that feels hung.

### Python, Bash, Awk — pluggable by design

"Code" shouldn't mean "only Python." A shell one-liner is the right action for some jobs; `awk` is unbeatable for others. So languages live in a **registry**, and the core loop dispatches by name without a single `if lang == ...` branch:

```python
class LanguageRegistry:
    def register(self, name: str, executor: LanguageExecutor) -> None:
        self._executors[name] = executor

    async def dispatch(self, name: str, code: str) -> SandboxResult:
        executor = self._executors.get(name)
        if executor is None:
            available = ", ".join(self.registered_languages()) or "(none)"
            return SandboxResult(stdout="", stderr="",
                exception=f"Language '{name}' is not registered. Available: {available}.")
        return await executor(code)
```

Adding a language is one `register()` call — no core changes. The design rule is that **Python is the stateful core** (it owns the persistent namespace), while subprocess languages like Bash and Awk are **stateless one-shots**, each a clean `subprocess.run`:

```python
class BashExecutor:
    async def __call__(self, code: str) -> SandboxResult:
        bash = require_tool("bash")    # resolve Git Bash / MSYS2 on PATH
        proc = subprocess.run([bash, "-c", code],
                              capture_output=True, text=True, timeout=30.0)
        return SandboxResult(stdout=proc.stdout, stderr=proc.stderr,
            exception=None if proc.returncode == 0 else f"Exit code: {proc.returncode}")
```

This is where "code, not just Python" earns its keep. When the job is "sum the byte counts in a multi-gigabyte access log," the agent doesn't pull the file into Python at all — it reaches for the tool built for exactly this:

```awk
{ total += $10 } END { print total }
```

One streaming pass, constant memory, and not a single byte of the log in the context window. The agent now picks the right tool for the job: pandas for a dataframe, `awk` or `grep` to stream a huge file, Python to glue the results together — all in the same session, all as code.

---

## CodeAct vs ReAct, head to head

Back to the opening task: *"find the three most expensive orders in each region."*

The ReAct trajectory sprawls:

```
Thought: Load the data.            Action: read_file("sales.csv")
Observation: <5000 rows of CSV dumped into context>
Thought: Filter to North.          Action: filter(region="North")
Observation: <North rows back into context>
Thought: Sort North by total.      Action: sort(by="total", desc=True)
Observation: <sorted North rows, again, into context>
Thought: Take the top 3.           Action: top_n(3)
Observation: <3 rows>
Thought: Now do South...           (repeat the whole cycle ×3 more regions)
Thought: Combine and summarize the twelve rows I'm holding.
Action: finish("...")
```

Every observation re-enters the window; the same rows are paid for several times; and the final summary is assembled from twelve rows the model is juggling in its head. The CodeAct trajectory is the five-line block from the introduction, executed once, with only the final 12-row table ever printed. Lay them side by side:

| | **ReAct / JSON tool-calling** | **CodeAct** |
|---|---|---|
| Actions to solve | `read_file` → `filter(North)` → `sort` → `top_n` → repeat ×4 regions → assemble in prose | one Python block |
| Where the GROUP BY happens | in the model's head, token by token | in pandas |
| Intermediate data | bounced through the context window each step | stays in the runtime as a variable |
| New capability needed? | hand-write each tool | `import pandas` |
| Failure mode | miscounts from memory; drifts over many turns | a traceback the agent fixes itself |

The ReAct version isn't wrong — it's *expensive*. Every round-trip spends tokens and invites drift, and the genuinely hard part (the aggregation) lands in the worst possible place: the model's free-text reasoning. The CodeAct version pushes the computation into a system built to compute, and uses the model for what it's good at — deciding *what* to compute. That's why the benchmark gap is real and not a rounding error: **74.4% vs 53.7%**, with **~30% fewer actions**.

---

## The limits of CodeAct

Now the honest part, because this is the wall that motivates everything that comes next.

**Every observation still lands in the context window.** CodeAct lets the agent compute over data without memorizing it — but the *result* it prints still flows back as tokens. Print too much and you flood the window. Our `truncate_output` is a band-aid over a structural fact: in plain CodeAct, the only way the model learns anything is by pulling it into context.

**Huge inputs don't fit.** A 5,000-row CSV is fine. A 5-million-row CSV, or a 300-page contract, or an entire codebase, does not fit in any context window. The agent can `read_csv` it into a *variable*, but the moment it needs to *see* a slice — to find the needle — that slice re-enters the window. Scale the input far enough and selective reading is the only option that survives.

**Large outputs blow the budget.** If the right answer is itself enormous — "rewrite all 200 of these files" — the model can't emit it token-by-token without paying for every token, twice.

**Security is real.** `exec()` runs arbitrary code. Our workspace is a working directory, not a hardened OS-level sandbox; production use needs genuine isolation (containers, gVisor, restricted interpreters). Power and danger are the same property here.

**Latency and nondeterminism.** Every turn is a model round-trip, and self-debug means several of them; a slow model makes the loop feel frozen. And the same prompt won't always produce the same code.

**The agent is only as good as the model's coding.** CodeAct hands the model a loaded weapon: arbitrary code. A strong model writes a clean `groupby` and finishes in one turn; a weaker one writes a subtly wrong block, misreads the traceback, and burns through the turn budget without converging. The paper's own numbers show this spread — the same action format that lifts a capable model can't rescue one that can't write the code in the first place. Self-debug is a powerful backstop, but it has a ceiling, and that ceiling is the model's fluency. There's a guardrail to set, too: the `max_turns` cap exists precisely so a confused agent fails fast instead of looping forever.

Most of these are engineering problems. But the first two — observations and inputs both forced through a finite context window — are *architectural*. You cannot truncate your way out of a 300-page document. You need a different relationship between the model and its data.

---

## What's next: Recursive Language Models

Here is a result from the agent we've *already* built on top of the CodeAct core above. It answered a question about a **233,325-character, 1,500-line corpus** — far larger than a comfortable prompt — and pinpointed the exact needle buried at line 534. The amount of that corpus that ever entered the model's context window? **About 240 characters.** It operated on a quarter-megabyte of text while *reading* two sentences of it.

The trick is a deceptively small change of stance: **stop putting the context *in* the model, and let the model *query* the context.** That is the core idea behind **Recursive Language Models (RLMs)** — context *by reference*, not by value. The large input never enters the prompt. It lives as an object the agent inspects with code: `context.search("invoice total")`, `context.lines(400, 450)`, `grep` over `context.path`. The model reads only the slices it asks for. And when a sub-problem deserves its own agent, the model spawns one with `llm_query(...)` — whose answer returns as a **Python variable in the parent's runtime, never injected into the parent's context.** Subagents compose through variables instead of bloating each other's windows.

That is the next article. We'll build the `ContextStore`, the recursive `llm_query` machinery, and the truncation discipline that makes it safe — and show how CodeAct, once it can call itself, stops being limited by the size of its window at all.

CodeAct widened the action space from a fixed menu to all of code. Recursive Language Models widen it again — to all of code, *including the model itself.* See you in Part 2.
