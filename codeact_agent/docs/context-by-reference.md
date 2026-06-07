# Context By Reference (Phase C)

## The Core Idea

The `context` variable in every agent session is no longer a plain string —
it is a **ContextStore** object backed by a file.

The key invariant: **the full text of a large context is NEVER dumped into
the LLM's message history.** The agent inspects it selectively by running
Python code (slicing, searching), and only what the agent explicitly
`print()`s reaches the LLM via the `<execution><stdout>` observation.

On top of that, all observations (stdout and stderr) are **truncated**
at 4 000 chars (head + tail with an omission marker), so even an accidental
`print(context)` cannot flood the window.

---

## The ContextStore API

```python
# --- Always available in the Python namespace ---
context           # ContextStore object (not a plain string)

# --- Representation ---
repr(context)     # "<Context: 4.2 MB, 51234 lines, file=/...>"
str(context)      # full text for SHORT inputs; summary repr for LARGE ones
len(context)      # approximate character count
context.size      # same as len(context)
context.path      # pathlib.Path to the backing file (for bash/awk)
context.workspace # pathlib.Path to the workspace directory

# --- Navigation ---
context.peek(n=200)        # first n characters
context.head(n=10)         # first n lines (joined by \n)
context.tail(n=10)         # last n lines
context.lines(start, end)  # line range (1-indexed, inclusive)
context[a:b]               # character slice
context[i]                 # single character

# --- Search ---
context.search("needle")             # substring search
context.search("needle", max_results=100)
context.grep(r"\bword\b")            # regex search
context.grep(r"pattern", max_results=50)
# Both return: [(line_number, line_text), ...]

# --- Workspace file helpers ---
context.write_file("output.txt", "results")
content = context.read_file("output.txt")
```

### Example: Find a needle in a large corpus

```python
# Check what we have
print(repr(context))
# <Context: 2.14 MB, 28000 lines, file=C:\...\.codeact_workspace\context.txt>

# Search for a pattern
matches = context.search("SECRET_NEEDLE")
for lineno, text in matches:
    print(f"Line {lineno}: {text}")
```

### Example: Process a large CSV in chunks

```python
# Read the header
header = context.head(1)
print(header)

# Read lines 1000-1100
chunk = context.lines(1000, 1100)
# Parse and process chunk ...
```

### Example: Use bash/awk over the context file

```python
# Get the file path
path = context.path
print(path)
```

Then in a bash block:
```bash
grep -n "error" /path/to/context.txt | head -20
```

Or:
```bash
awk 'NR==100,NR==200' /path/to/context.txt
```

---

## Loading a Large Context

### From the CLI: `/load <path>`

```
codeact> /load /data/big_corpus.txt
Loaded: big_corpus.txt (2.14 MB, 28000 lines)
  Spill path: C:\Users\...\AppData\Local\Temp\codeact_...\context.txt
```

After `/load`, the context is **locked**: the loop's per-turn automatic
`set_context(user_query)` calls are no-ops, so your file stays in `context`
across multiple turns. Use `/reset` to clear and unlock.

### Programmatically (in tests / scripts)

```python
from pathlib import Path
from codeact_agent.sandbox import PythonSandbox

sandbox = PythonSandbox()
# From a file (lock=True prevents the loop overwriting it):
sandbox.set_context(Path("/data/corpus.txt"), lock=True)

# From a large string (same API):
sandbox.set_context(big_text, lock=True)
```

### Backward Compatibility

For short strings (< 10 000 chars), `str(context)` still returns the full
text, so all Phase A / B code that does `print(context)` after a short
`set_context("my query")` continues to work exactly as before.

---

## Output Truncation

All stdout and stderr fed back to the LLM is capped at **4 000 chars**
(configurable via `AgentLoop(output_max_chars=N)`).

When output exceeds the cap, the middle is replaced with:
```
… [N chars omitted] …
```

The LLM always sees the head and tail. The exception traceback is NOT
truncated (it is generally small and always useful for debugging).

This means even an accidental `print(context)` in agent code is safe:
for large inputs `str(context)` returns a short summary, and even if it
returned the full text it would be truncated by the observation formatter.

---

## Workspace

The workspace is a directory (default: a temporary directory created by
`tempfile.mkdtemp(prefix="codeact_")`) that holds:
- `context.txt` — the spill file for the current context
- Any files written by agent code via `context.write_file()`

Agent code can also read/write the workspace using plain Python or bash,
rooted at `context.workspace`.

**Safety note:** This is NOT a hard OS-level security sandbox. Agent code
can read and write anywhere the process has permissions. The workspace is
just a working directory convention.

---

## System Prompt Guidance

The system prompt (in `prompts.py`) instructs the agent:

1. Never do `print(context)` on a large context — it returns only a summary.
2. Always inspect context selectively: `context.search`, `context.head`,
   `context.lines`, slicing.
3. Use bash/awk blocks over `context.path` for Unix tool pipelines.
4. Combine `context.lines(...)` + `llm_query(slice)` for very large analysis.
5. Observations are truncated — print only what is needed.
