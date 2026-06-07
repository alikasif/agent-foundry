# Language Registry — Adding and Using Languages

## Built-in languages

| Name     | Persistence | Resolver | Notes |
|----------|-------------|----------|-------|
| `python` | STATEFUL    | in-process | Primary language; namespace persists across turns |
| `bash`   | stateless   | Git Bash / MSYS2 | One-shot subprocess; 30 s timeout |
| `awk`    | stateless   | Git Bash / MSYS2 | One-shot subprocess; 30 s timeout |
| `text`   | stateless   | Git Bash / MSYS2 | Alias for bash; fallback for unlabelled blocks |

JavaScript (`js`, `node`) and SQL (`sqlite`) are intentionally NOT registered
by default — they are optional plugins (see below).

## How dispatch works

1. The LLM writes a fenced code block with a language tag:
   ` ```python `, ` ```bash `, ` ```awk `.
2. `extract_code_blocks()` normalises aliases (`sh → bash`, `py → python`,
   `gawk → awk`, …) and returns a list of `(language, code)` tuples.
3. `AgentLoop.run_turn()` calls `registry.dispatch(lang, code)` for each block.
4. The registry looks up the executor by name and awaits `executor(code)`.
5. Unknown language names return a `SandboxResult` with a descriptive error
   in `exception` — no exception is raised in the loop.

## Adding a new language

Implement the `LanguageExecutor` protocol:

```python
from codeact_agent.languages.registry import LanguageExecutor
from codeact_agent.sandbox import SandboxResult

class RubyExecutor:
    async def __call__(self, code: str) -> SandboxResult:
        import subprocess
        proc = subprocess.run(
            ["ruby", "-e", code],
            capture_output=True, text=True, timeout=30,
        )
        return SandboxResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exception=None if proc.returncode == 0 else f"Exit code: {proc.returncode}",
            return_value=None,
        )
```

Then register it — before or after the loop is created:

```python
from codeact_agent.languages import register

register("ruby", RubyExecutor())
```

Or register in a specific registry instance:

```python
from codeact_agent.languages.default import build_default_registry
from codeact_agent.sandbox import PythonSandbox

sandbox = PythonSandbox()
registry = build_default_registry(sandbox)
registry.register("ruby", RubyExecutor())
```

No core loop code needs to change.  The loop dispatches by name.

## Optional plugins (JS / SQL)

### JavaScript (Node.js)

```python
import subprocess
from codeact_agent.languages import register
from codeact_agent.sandbox import SandboxResult

class NodeExecutor:
    async def __call__(self, code: str) -> SandboxResult:
        try:
            proc = subprocess.run(
                ["node", "-e", code],
                capture_output=True, text=True, timeout=30,
            )
            return SandboxResult(
                stdout=proc.stdout, stderr=proc.stderr,
                exception=None if proc.returncode == 0 else f"Exit: {proc.returncode}",
            )
        except FileNotFoundError:
            return SandboxResult(
                stdout="", stderr="",
                exception="Node.js not found. Install from https://nodejs.org",
            )

register("javascript", NodeExecutor())
register("js", NodeExecutor())     # alias
register("node", NodeExecutor())   # alias
```

### SQL (sqlite3)

```python
import subprocess
from codeact_agent.languages import register
from codeact_agent.sandbox import SandboxResult

class SQLiteExecutor:
    async def __call__(self, code: str) -> SandboxResult:
        try:
            proc = subprocess.run(
                ["sqlite3", ":memory:", code],
                capture_output=True, text=True, timeout=30,
            )
            return SandboxResult(
                stdout=proc.stdout, stderr=proc.stderr,
                exception=None if proc.returncode == 0 else f"Exit: {proc.returncode}",
            )
        except FileNotFoundError:
            return SandboxResult(
                stdout="", stderr="",
                exception="sqlite3 not found.",
            )

register("sql", SQLiteExecutor())
register("sqlite", SQLiteExecutor())
```

## Git Bash resolver

On Windows, `bash` and `awk` are not native.  The resolver
(`bash_resolver.py`) searches:

1. PATH (covers Git for Windows installed to PATH, MSYS2, Cygwin)
2. `C:\Program Files\Git\usr\bin`
3. `C:\Program Files (x86)\Git\usr\bin`
4. `C:\msys64\usr\bin`, `C:\msys32\usr\bin`, `C:\msys2\usr\bin`

Results are cached with `@functools.cache` after the first lookup.

If a tool is missing, `BashExecutor` / `AwkExecutor` return a
`SandboxResult` with an actionable error in `exception`:

```
'bash' not found on PATH or in known Git-for-Windows / MSYS2 locations.
To fix this, install one of:
  - Git for Windows: https://git-scm.com/download/win
  - MSYS2: https://www.msys2.org/
Then restart your terminal so the new PATH is visible.
```

To add search paths, extend `_EXTRA_SEARCH_PATHS` in `bash_resolver.py`.
To force a re-scan after PATH changes, call `bash_resolver.clear_cache()`.
