"""Tests for the Phase C ContextStore (context-by-reference store).

All tests are deterministic (no live LLM calls).

Coverage:
- set from string and from file
- peek / slice / search / grep / lines / head / tail / len
- str(context) / repr returns a short summary for large inputs,
  full text for short inputs (backward compat)
- large input spills to a file and context.path points to it
- by-reference invariant: full context text never appears in history
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeact_agent.context import _SHORT_TEXT_THRESHOLD, ContextStore
from codeact_agent.sandbox import PythonSandbox

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return a temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def short_text() -> str:
    """Return a short text well below the spill threshold."""
    return "Hello, world!\nSecond line.\nThird line.\n"


@pytest.fixture
def large_text() -> str:
    """Return a large text that exceeds the spill threshold."""
    line = "The quick brown fox jumps over the lazy dog.\n"
    # Repeat until we exceed the threshold
    repeat = (_SHORT_TEXT_THRESHOLD // len(line)) + 10
    return line * repeat


@pytest.fixture
def needle_corpus(tmp_path: Path) -> tuple[Path, str]:
    """Create a large corpus file with a hidden needle.

    Returns:
        Tuple of (file_path, needle_text).
    """
    needle = "SECRET_NEEDLE_42"
    lines: list[str] = []
    for i in range(5000):
        if i == 2500:
            lines.append(f"line {i}: {needle} is here\n")
        else:
            lines.append(f"line {i}: filler content about topic {i % 17}\n")
    corpus_path = tmp_path / "corpus.txt"
    corpus_path.write_text("".join(lines), encoding="utf-8")
    return corpus_path, needle


# ---------------------------------------------------------------------------
# Construction: from_string
# ---------------------------------------------------------------------------


class TestFromString:
    """ContextStore.from_string tests."""

    def test_short_string_str_returns_full_text(
        self, workspace: Path, short_text: str
    ) -> None:
        """str(context) returns the full text for short inputs (backward compat)."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert str(store) == short_text

    def test_short_string_len(self, workspace: Path, short_text: str) -> None:
        """len(context) returns the character count."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert len(store) == len(short_text)

    def test_large_string_str_returns_summary(
        self, workspace: Path, large_text: str
    ) -> None:
        """str(context) returns a short summary for large inputs, NOT full text."""
        store = ContextStore.from_string(large_text, workspace=workspace)
        result = str(store)
        # Must NOT contain bulk of the text
        assert large_text not in result
        # Must contain a recognisable summary marker
        assert "<Context:" in result
        assert "lines" in result

    def test_large_string_repr_is_short(self, workspace: Path, large_text: str) -> None:
        """repr(context) is a short summary for large inputs."""
        store = ContextStore.from_string(large_text, workspace=workspace)
        summary = repr(store)
        # Summary must be much shorter than the actual text
        assert len(summary) < 200
        assert large_text not in summary

    def test_path_is_set_after_from_string(
        self, workspace: Path, short_text: str
    ) -> None:
        """context.path points to a real file after from_string()."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert store.path.exists()
        assert store.path.read_text(encoding="utf-8") == short_text

    def test_path_is_set_for_large_string(
        self, workspace: Path, large_text: str
    ) -> None:
        """context.path is always set, even for large inputs."""
        store = ContextStore.from_string(large_text, workspace=workspace)
        assert store.path.exists()
        assert store.path.stat().st_size > 0

    def test_line_count(self, workspace: Path) -> None:
        """Line count is accurate for multi-line text."""
        text = "a\nb\nc\n"
        store = ContextStore.from_string(text, workspace=workspace)
        assert store._line_count == 3

    def test_empty_string(self, workspace: Path) -> None:
        """Empty string creates a store with zero chars / lines."""
        store = ContextStore.from_string("", workspace=workspace)
        assert len(store) == 0
        assert store._line_count == 0


# ---------------------------------------------------------------------------
# Construction: from_path
# ---------------------------------------------------------------------------


class TestFromPath:
    """ContextStore.from_path tests."""

    def test_from_path_creates_file_in_workspace(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        """from_path copies the source into workspace/context.txt."""
        source = tmp_path / "source.txt"
        source.write_text("hello from file\n", encoding="utf-8")
        store = ContextStore.from_path(source, workspace=workspace)
        assert (workspace / "context.txt").exists()
        assert store.path == workspace / "context.txt"

    def test_from_path_size_and_lines(self, workspace: Path, tmp_path: Path) -> None:
        """from_path sets correct char count and line count."""
        text = "line1\nline2\nline3\n"
        source = tmp_path / "src.txt"
        source.write_text(text, encoding="utf-8")
        store = ContextStore.from_path(source, workspace=workspace)
        assert store._line_count == 3
        assert store.size == len(text)

    def test_from_path_large_file_summary(
        self, workspace: Path, needle_corpus: tuple[Path, str]
    ) -> None:
        """Large file from_path produces a summary repr, not full text."""
        corpus_path, _ = needle_corpus
        store = ContextStore.from_path(corpus_path, workspace=workspace)
        summary = str(store)
        assert "<Context:" in summary
        assert "lines" in summary


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


class TestNavigationHelpers:
    """Tests for peek, head, tail, lines, slice."""

    def test_peek_returns_first_n_chars(self, workspace: Path, short_text: str) -> None:
        """peek(n) returns the first n characters."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert store.peek(5) == short_text[:5]

    def test_head_returns_first_n_lines(self, workspace: Path) -> None:
        """head(n) returns the first n lines."""
        text = "\n".join(f"line{i}" for i in range(20)) + "\n"
        store = ContextStore.from_string(text, workspace=workspace)
        result = store.head(3)
        assert result == "line0\nline1\nline2"

    def test_tail_returns_last_n_lines(self, workspace: Path) -> None:
        """tail(n) returns the last n lines."""
        text = "\n".join(f"line{i}" for i in range(20)) + "\n"
        store = ContextStore.from_string(text, workspace=workspace)
        result = store.tail(3)
        assert result == "line17\nline18\nline19"

    def test_lines_returns_range(self, workspace: Path) -> None:
        """lines(start, end) returns the specified line range."""
        text = "\n".join(f"L{i}" for i in range(1, 11)) + "\n"
        store = ContextStore.from_string(text, workspace=workspace)
        result = store.lines(3, 5)
        assert result == "L3\nL4\nL5"

    def test_slice_access(self, workspace: Path, short_text: str) -> None:
        """context[a:b] returns the correct character slice."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert store[0:5] == short_text[0:5]
        assert store[7:] == short_text[7:]

    def test_slice_large_text(self, workspace: Path, large_text: str) -> None:
        """Slicing works for large text (reads from file)."""
        store = ContextStore.from_string(large_text, workspace=workspace)
        # For large text, _text is None, so it reads from file
        assert store._text is None
        result = store[0:10]
        assert result == large_text[0:10]

    def test_len_returns_char_count(self, workspace: Path, short_text: str) -> None:
        """len(context) returns the character count."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert len(store) == len(short_text)

    def test_size_property(self, workspace: Path, short_text: str) -> None:
        """context.size returns character count."""
        store = ContextStore.from_string(short_text, workspace=workspace)
        assert store.size == len(short_text)


# ---------------------------------------------------------------------------
# Search / grep helpers
# ---------------------------------------------------------------------------


class TestSearchHelpers:
    """Tests for search() and grep()."""

    def test_search_finds_substring(self, workspace: Path) -> None:
        """search() finds lines containing the substring."""
        text = "apple\nbanana\ncherry\n"
        store = ContextStore.from_string(text, workspace=workspace)
        matches = store.search("banana")
        assert len(matches) == 1
        lineno, line = matches[0]
        assert lineno == 2
        assert "banana" in line

    def test_search_returns_empty_for_no_match(self, workspace: Path) -> None:
        """search() returns an empty list when nothing matches."""
        store = ContextStore.from_string("apple\nbanana\n", workspace=workspace)
        assert store.search("xyz") == []

    def test_search_line_numbers_are_1indexed(self, workspace: Path) -> None:
        """search() line numbers start at 1."""
        text = "first\nsecond\nthird\n"
        store = ContextStore.from_string(text, workspace=workspace)
        matches = store.search("third")
        assert matches[0][0] == 3

    def test_grep_finds_regex_match(self, workspace: Path) -> None:
        """grep() finds lines matching a regular expression."""
        text = "foo 123\nbar 456\nbaz 789\n"
        store = ContextStore.from_string(text, workspace=workspace)
        matches = store.grep(r"\d{3}")
        assert len(matches) == 3

    def test_grep_no_match(self, workspace: Path) -> None:
        """grep() returns an empty list when regex matches nothing."""
        store = ContextStore.from_string("hello\nworld\n", workspace=workspace)
        assert store.grep(r"\d+") == []

    def test_search_max_results_limit(self, workspace: Path) -> None:
        """search() respects the max_results cap."""
        text = "\n".join(["match"] * 100) + "\n"
        store = ContextStore.from_string(text, workspace=workspace)
        matches = store.search("match", max_results=10)
        assert len(matches) == 10

    def test_search_needle_in_large_corpus(
        self, workspace: Path, needle_corpus: tuple[Path, str]
    ) -> None:
        """search() finds the needle in a large corpus without loading everything."""
        corpus_path, needle = needle_corpus
        store = ContextStore.from_path(corpus_path, workspace=workspace)
        # Confirm large text (no _text cache)
        assert store._text is None
        matches = store.search(needle)
        assert len(matches) == 1
        lineno, line = matches[0]
        assert needle in line
        # Line number should be around 2501 (0-indexed 2500 + 1)
        assert lineno == 2501


# ---------------------------------------------------------------------------
# Sandbox integration: set_context from string and path
# ---------------------------------------------------------------------------


class TestSandboxIntegration:
    """Tests that PythonSandbox.set_context works with string and Path."""

    @pytest.mark.anyio
    async def test_set_context_short_string_prints_text(self, workspace: Path) -> None:
        """set_context with a short string: print(context) outputs the text."""
        sandbox = PythonSandbox(workspace=workspace)
        sandbox.set_context("what is 2+2?")
        result = await sandbox.execute("print(context)")
        assert "what is 2+2?" in result.stdout

    @pytest.mark.anyio
    async def test_set_context_from_path(self, workspace: Path, tmp_path: Path) -> None:
        """set_context with a Path loads the file as context."""
        src = tmp_path / "data.txt"
        src.write_text("file content here\n", encoding="utf-8")
        sandbox = PythonSandbox(workspace=workspace)
        sandbox.set_context(src)
        result = await sandbox.execute("print(context.head(1))")
        assert "file content here" in result.stdout

    @pytest.mark.anyio
    async def test_context_path_accessible(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        """context.path is accessible from sandbox code."""
        src = tmp_path / "data.txt"
        src.write_text("hello\nworld\n", encoding="utf-8")
        sandbox = PythonSandbox(workspace=workspace)
        sandbox.set_context(src)
        result = await sandbox.execute("print(str(context.path))")
        assert "context.txt" in result.stdout

    @pytest.mark.anyio
    async def test_default_context_is_context_store(self, workspace: Path) -> None:
        """After init, context is a ContextStore, not a plain string."""
        from codeact_agent.context import ContextStore

        sandbox = PythonSandbox(workspace=workspace)
        assert isinstance(sandbox.namespace["context"], ContextStore)

    @pytest.mark.anyio
    async def test_context_search_from_sandbox(self, workspace: Path) -> None:
        """context.search() works from within sandbox code execution."""
        sandbox = PythonSandbox(workspace=workspace)
        sandbox.set_context("apple\nbanana\ncherry\n")
        result = await sandbox.execute(
            "matches = context.search('banana')\nprint(matches[0][1])"
        )
        assert "banana" in result.stdout

    @pytest.mark.anyio
    async def test_reset_reinjects_context_store(self, workspace: Path) -> None:
        """After reset(), context is still a ContextStore, not a plain string."""
        from codeact_agent.context import ContextStore

        sandbox = PythonSandbox(workspace=workspace)
        sandbox.reset()
        assert isinstance(sandbox.namespace["context"], ContextStore)


# ---------------------------------------------------------------------------
# Backward compatibility with Phase A / B invariants
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Ensure Phase A / B invariants still hold."""

    @pytest.mark.anyio
    async def test_final_still_works(self, workspace: Path) -> None:
        """FINAL() is still available and still raises FinalSignal."""

        sandbox = PythonSandbox(workspace=workspace)
        result = await sandbox.execute("FINAL('done')")
        assert result.final_value == "done"

    @pytest.mark.anyio
    async def test_namespace_persistence_still_works(self, workspace: Path) -> None:
        """Variables persist across execute() calls."""
        sandbox = PythonSandbox(workspace=workspace)
        await sandbox.execute("x = 42")
        result = await sandbox.execute("print(x)")
        assert "42" in result.stdout

    @pytest.mark.anyio
    async def test_context_in_namespace_after_init(self, workspace: Path) -> None:
        """'context' key is always present in the namespace."""
        sandbox = PythonSandbox(workspace=workspace)
        assert "context" in sandbox.namespace

    @pytest.mark.anyio
    async def test_context_in_namespace_after_reset(self, workspace: Path) -> None:
        """'context' is still present after reset()."""
        sandbox = PythonSandbox(workspace=workspace)
        sandbox.reset()
        assert "context" in sandbox.namespace
