# Subagents — Recursive LLM Queries

Phase B adds two Python builtins to the CodeAct agent namespace that let the
agent decompose tasks into child agents and compose results without context
bloat.

---

## The core invariant

> **Subagent results are returned as Python values bound in the parent's REPL
> namespace. They are NOT appended to the parent's LLM message history.**
>
> The parent inspects a subagent result only by choosing to `print()` it (or
> slice it) in a later Python block.

### Why this matters — context bloat vs. value passing

The naive approach to subagents pipes the child's full reasoning trace back
into the parent's message history. For three children each generating 500
tokens of reasoning, the parent's context grows by 1500 tokens before it even
starts composing. This slows inference, increases cost, and can hit context
limits.

The RLM approach: the child's answer is a **Python variable**. The parent LLM
sees only what it explicitly `print()`s — the observation is lean, structured
data, not the child's raw transcript.

| Approach | Parent sees | Tokens used |
|---|---|---|
| Context-append | Full child reasoning trace | High |
| **RLM (this agent)** | Only what `print()` emits | Minimal |

---

## `llm_query(prompt, context=None)`

Spawns a fresh child CodeAct agent and returns its final answer as a string.

```python
# The result is bound to FRUIT — no trace enters parent history.
FRUIT = llm_query("List 3 tropical fruits, one per line.")

# Nothing reaches the parent LLM yet.
# Only when you print does the value become part of the observation:
print(FRUIT)
```

**Parameters:**

- `prompt` (`str`): The task description for the child agent.
- `context` (`str | None`): Optional supplementary context prepended to the
  prompt. Use this to pass large inputs the child needs without growing the
  parent system prompt.

**Returns:** `str` — the child's final answer.

**Raises:**

- `SubagentDepthError` — if the call would exceed the maximum recursion depth
  (default: 3).
- `RuntimeError` — if no `SubagentRunner` is wired into the sandbox (e.g., in
  tests that construct `PythonSandbox()` directly without a runner).

---

## `llm_query_many(prompts, contexts=None)`

Runs multiple child agents **in parallel** and returns answers as a list,
aligned with `prompts`.

```python
results = llm_query_many([
    "List 3 tropical fruits.",
    "List 3 European countries.",
    "List 3 large animals.",
])

# results[0] = fruits answer
# results[1] = countries answer
# results[2] = animals answer

combined = {
    "fruits":    results[0],
    "countries": results[1],
    "animals":   results[2],
}
FINAL(combined)
```

**Parameters:**

- `prompts` (`list[str]`): One prompt per child agent.
- `contexts` (`list[str | None] | None`): Optional per-prompt context strings.
  Must be the same length as `prompts`, or omit entirely for no context.

**Returns:** `list[str]` — answers in the same order as `prompts`.

---

## Recursion limits

| Setting | Default | Notes |
|---|---|---|
| `max_depth` | 3 | Depth of nesting: root=0, first child=1, grandchild=2, etc. |
| `child_max_turns` | 8 | Self-debug iterations per child turn (parent uses 20). |

Exceeding `max_depth` raises `SubagentDepthError`. Agent code can catch it:

```python
try:
    answer = llm_query("deeply nested task")
except Exception as e:
    print(f"Subagent failed: {e}")
    answer = "unavailable"
```

---

## Parallelism and thread safety

`llm_query_many` runs each child in its own **worker thread**, each with its
own `asyncio.run()` event loop. This is safe because:

- Children are spawned from `exec()` which runs synchronously on the main loop
  thread. Calling `asyncio.run()` from within a running loop raises
  `RuntimeError` — the worker thread sidesteps this.
- Each child has a **fresh** `PythonSandbox` with its own namespace. There is
  no shared state between siblings.

**Known limitation**: Child sandboxes use in-process `sys.stdout` redirection.
Parallel children running concurrently may interleave their `sys.stdout`
assignments. This is safe because child answers flow back via their `FINAL()`
return value, not via stdout capture. Do not rely on child `print()` output
being captured correctly when running in parallel.

---

## KV-cache friendliness

All child agents use the same `CODEACT_SYSTEM_PROMPT` as the parent. Provider
prefix caching (e.g. OpenRouter) can cache the common system prompt across all
sibling calls in a `llm_query_many()` batch, reducing cost for the second and
subsequent children.

---

## Worked example — fruits, countries, animals

This example mirrors the article's motivating pattern: decompose into three
parallel sub-tasks, compose the results without re-generating data.

```python
results = llm_query_many([
    "Name exactly 3 tropical fruits. Reply with one per line, no numbering.",
    "Name exactly 3 European countries. One per line, no numbering.",
    "Name exactly 3 large animals. One per line, no numbering.",
])

fruits    = [f.strip() for f in results[0].splitlines() if f.strip()]
countries = [c.strip() for c in results[1].splitlines() if c.strip()]
animals   = [a.strip() for a in results[2].splitlines() if a.strip()]

summary = {
    "fruits":    fruits,
    "countries": countries,
    "animals":   animals,
}

FINAL(summary)
```

The parent LLM never processes the children's raw text. It only sees the
composed `summary` dict via `FINAL()`.

---

## How children are constructed

Each child gets:

- The same **model**, **API key**, and **base URL** as the parent.
- A **fresh `PythonSandbox`** (empty namespace, its own `FINAL`, `llm_query`, etc.).
- A **child `SubagentRunner`** with `depth = parent_depth + 1` — so
  grandchildren are capped automatically.
- No streaming callbacks — children run silently. A dim progress note
  (`↳ subagent (depth N): …`) is printed to the parent console.

---

## See also

- `docs/architecture.md` — overall system design
- `docs/languages.md` — language registry (subagents are NOT a new language)
- `src/codeact_agent/subagents.py` — `SubagentRunner` implementation
- `src/codeact_agent/sandbox.py` — `llm_query`/`llm_query_many` injection
