# Review: AgentLoop Implementation (T011)

**Status:** APPROVED
**Severity:** N/A

## Reviewed Files

- `src/codeact_agent/loop.py` (101 lines)
- `tests/test_loop.py` (548 lines, 43 tests)

---

## Checklist Ratings

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Loop logic correctness (extract -> execute -> observe -> repeat) | PASS | The `run_turn` method correctly implements the self-debug cycle: call agent, extract code blocks, execute each, format observation, append to history, repeat until no code or max_turns. |
| History management (observations appended, original not mutated) | PASS | `current_history = list(history)` creates a shallow copy on entry. Each observation turn also does `current_history = list(current_history)` before appending. Test `test_original_history_not_mutated` verifies the original is preserved. |
| Max turns guard | PASS | `for _turn in range(self._max_turns)` correctly limits iterations. Tests verify exact call counts for max_turns=5 and max_turns=1. |
| Code extraction edge cases (regex correctness) | PASS | 9 tests cover: single block, multiple blocks, no blocks, non-python blocks, mixed blocks, empty string, multiline content, leading whitespace stripping, blank lines inside blocks. The regex ` ```python\s*\n(.*?)``` ` with `re.DOTALL` is correct. |
| Observation format (XML-tagged) | PASS | `_format_observation` produces `<execution><stdout>...</stdout><stderr>...</stderr><exception>...</exception></execution>`. Tests verify all XML tags are present. |
| Reset delegation to sandbox | PASS | `AgentLoop.reset()` calls `self._sandbox.reset()`. Test verifies delegation with mock. Integration test verifies real sandbox namespace is cleared. |
| Test coverage (43 tests) | PASS | 43 tests across 8 test classes: TestExtractCodeBlocks (9), TestAgentLoopInit (2), TestSingleTurnExecution (3), TestMultiTurnSelfDebug (2), TestNoCodeResponse (2), TestMaxTurnsGuard (2), TestHistoryAccumulation (3), TestReset (1), TestRealSandboxIntegration (3). Plus additional tests in the classes. All use anyio. |
| Type hints | PASS | All functions and methods have complete type hints: `extract_code_blocks(text: str) -> list[str]`, `run_turn(self, history: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]`, etc. |
| Docstrings | PASS | Module docstring, class docstring, all public and private method docstrings present. `extract_code_blocks` has Args/Returns sections. |
| Ruff compliance | PASS | Code follows PEP 8, line length <= 88, no unused imports, clean formatting. |

---

## Detailed Analysis

### Loop Logic Correctness

The `run_turn` method (lines 69-96) implements the correct flow:
1. Copies history to avoid mutation
2. Loops up to `max_turns` times
3. Calls `agent.run(current_history)` to get response
4. Extracts code blocks; if none, returns immediately (early return pattern -- good)
5. For each code block, executes in sandbox, formats observation, appends to history
6. After exhausting max_turns, returns the last response

This matches the PRD FR-3 specification and the plan.md data flow exactly.

### History Management

The implementation correctly:
- Creates a shallow copy of the input history at the start (`current_history = list(history)`)
- Creates another copy before appending each observation (`current_history = list(current_history)` before `current_history.append(...)`)
- Returns the updated history alongside the response

The test `test_original_history_not_mutated` explicitly verifies the input list is not modified.

### Max Turns Guard

The `for _turn in range(self._max_turns)` loop correctly terminates. When the loop exhausts all turns, the last `response` value (from the final `agent.run()` call) is returned. Tests verify exact agent call counts match the max_turns parameter.

### Code Extraction

The regex `_CODE_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)` correctly:
- Matches only ` ```python ` fenced blocks (ignores javascript, bash, etc.)
- Uses non-greedy `.*?` with `re.DOTALL` to handle multiline content
- Strips leading/trailing whitespace from extracted content via `.strip()`

Edge cases are thoroughly tested.

### Observation Format

The `_format_observation` function (lines 25-45) produces the XML-tagged format specified in plan.md section 3.5:
```
<execution>
<stdout>
...
</stdout>
<stderr>
...
</stderr>
<exception>
...
</exception>
</execution>
```

The format handles empty stdout/stderr gracefully (uses `""` when the value is falsy).

### Reset Delegation

`AgentLoop.reset()` (line 98-100) correctly delegates to `self._sandbox.reset()`. Both mock-based and real-sandbox integration tests verify this.

### Test Coverage

43 tests with excellent coverage across:
- **Unit tests with mocks**: Fast, deterministic tests for all loop logic paths
- **Integration tests with real sandbox**: 3 tests verify end-to-end behavior with actual code execution
- **Edge cases**: Empty input, no code blocks, max_turns=1, history mutation check

All tests use `@pytest.mark.anyio` for async execution.

### Code Quality

- Clean, flat control flow with early return (no deeply nested conditionals)
- No unnecessary abstractions or defensive programming
- Single responsibility: `AgentLoop` orchestrates, `extract_code_blocks` parses, `_format_observation` formats
- Private attributes prefixed with underscore
- Module-level compiled regex for performance

---

## Issues Found

**None.** All checklist items pass. The implementation is clean, well-tested, and fully compliant with the PRD and plan.md specifications.

---

## Security Notes

**None identified.** The loop itself does not handle secrets or perform I/O directly. Code execution is delegated to `PythonSandbox`, which is reviewed separately (T010).

---

## Positive Notes

- Excellent test coverage (43 tests) with both mock-based unit tests and real-sandbox integration tests
- Clean early-return pattern in `run_turn` (returns immediately when no code blocks found)
- History immutability is correctly handled and explicitly tested
- The `_format_observation` helper is a clean separation of concerns
- Compiled module-level regex (`_CODE_BLOCK_RE`) avoids recompilation on each call
- Type hints are complete and correct throughout
- Docstrings follow Google-style with Args/Returns sections
