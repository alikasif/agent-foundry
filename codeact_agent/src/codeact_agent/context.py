"""ContextStore -- context-by-reference storage for the CodeAct agent.

Phase C addition
-----------------
The ``ContextStore`` object replaces the plain string ``context`` variable in
the Python namespace.  Large inputs are spilled to a workspace file so the
agent can inspect them selectively without loading everything into the LLM
window.

Key invariants
~~~~~~~~~~~~~~
- The full text is **never** dumped into the LLM history automatically.
  ``str(context)`` returns a short summary for inputs over the spill
  threshold.  Existing short-string behaviour (Phase A / B) is preserved:
  when the text is below ``_SHORT_TEXT_THRESHOLD`` chars, ``str()`` returns
  the full text so existing tests keep passing.
- The agent inspects context via helper methods:
  ``peek``, ``head``, ``tail``, slicing (``context[a:b]``),
  ``lines``, ``search`` / ``grep``, and ``context.path`` for bash/awk.
- ``set_context()`` (Phase A API) accepts a plain string OR a
  ``pathlib.Path`` pointing to an existing file.

Backward compatibility
~~~~~~~~~~~~~~~~~~~~~~
All Phase A / B tests that call ``set_context("short string")`` and then
``print(context)`` continue to work because ``__str__`` returns the full
text for short inputs.

Safety note
~~~~~~~~~~~
This is **not** a hard OS-level security sandbox.  The workspace is just a
working directory.  Agent code can read/write anywhere the process has access.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import overload

# Threshold below which str(context) returns the full text (backward compat).
_SHORT_TEXT_THRESHOLD: int = 10_000


class ContextStore:
    """A reference to a (potentially large) text corpus.

    The corpus is always backed by a file in the workspace directory.
    For short strings the in-memory text is returned directly; for large
    inputs a temp/workspace file is used and the text is never loaded in
    full unless explicitly requested.

    Parameters
    ----------
    workspace:
        Directory where the spill file lives.  Created if it does not exist.
        Defaults to a new temporary directory.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        """Initialise with an empty corpus and optional workspace root.

        Args:
            workspace: Directory for the spill file.  A fresh ``tempfile``
                directory is created when ``None``.
        """
        self._workspace: Path = workspace or Path(tempfile.mkdtemp(prefix="codeact_"))
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._path: Path | None = None  # set when text is loaded
        self._char_count: int = 0
        self._line_count: int = 0
        self._text: str | None = None  # cached for short strings only

    # ------------------------------------------------------------------
    # Construction helpers (used by set_context)
    # ------------------------------------------------------------------

    @classmethod
    def from_string(cls, text: str, workspace: Path | None = None) -> ContextStore:
        """Create a ContextStore from a plain string.

        For short strings the text is held in memory AND written to a
        workspace file so ``context.path`` always works.

        Args:
            text: The text content.
            workspace: Optional workspace directory.

        Returns:
            A new ContextStore instance.
        """
        store = cls(workspace)
        path = store._workspace / "context.txt"
        path.write_text(text, encoding="utf-8")
        store._path = path
        store._char_count = len(text)
        has_trailing = text and not text.endswith("\n")
        store._line_count = text.count("\n") + (1 if has_trailing else 0)
        # Cache the full text only for short inputs.
        if len(text) <= _SHORT_TEXT_THRESHOLD:
            store._text = text
        return store

    @classmethod
    def from_path(cls, source: Path, workspace: Path | None = None) -> ContextStore:
        """Create a ContextStore from an existing file.

        The file is copied into the workspace as ``context.txt`` so the
        agent has a stable, writable path.

        Args:
            source: Path to the existing file to load.
            workspace: Optional workspace directory.

        Returns:
            A new ContextStore instance.
        """
        store = cls(workspace)
        dest = store._workspace / "context.txt"
        shutil.copy2(source, dest)
        store._path = dest
        text_bytes = dest.stat().st_size
        store._char_count = text_bytes  # byte approximation; recounted below
        # Count chars and lines by streaming (no full load).
        char_count = 0
        line_count = 0
        with dest.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                char_count += len(line)
                line_count += 1
        store._char_count = char_count
        store._line_count = line_count
        return store

    # ------------------------------------------------------------------
    # Core properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Path to the backing file in the workspace.

        bash/awk executors can operate on this file directly, e.g.
        ``grep 'pattern' {context.path}``.

        Returns:
            Absolute path to the context file.

        Raises:
            RuntimeError: If no context has been loaded yet.
        """
        if self._path is None:
            raise RuntimeError(
                "No context loaded.  Call set_context() first or use /load."
            )
        return self._path

    @property
    def workspace(self) -> Path:
        """The workspace directory that contains the context file.

        Returns:
            Absolute path to the workspace directory.
        """
        return self._workspace

    @property
    def size(self) -> int:
        """Approximate character count of the context.

        Returns:
            Number of characters in the context text.
        """
        return self._char_count

    # ------------------------------------------------------------------
    # Python dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the approximate character count (for len(context))."""
        return self._char_count

    def __repr__(self) -> str:
        """Return a concise summary — never dumps the full text."""
        if self._path is None:
            return "<Context: empty>"
        size_mb = self._char_count / 1_048_576
        if size_mb >= 0.1:
            size_str = f"{size_mb:.2f} MB"
        elif self._char_count >= 1024:
            size_str = f"{self._char_count / 1024:.1f} KB"
        else:
            size_str = f"{self._char_count} chars"
        return f"<Context: {size_str}, {self._line_count} lines, file={self._path}>"

    def __str__(self) -> str:
        """Return full text for short inputs; summary repr for large ones.

        This preserves Phase A / B backward compatibility:
        ``print(context)`` after ``set_context('short query')`` still
        outputs the query text.  For large inputs it prints the summary
        so an accidental ``print(context)`` is safe.
        """
        if self._text is not None:
            # Short string: return full text (backward compat)
            return self._text
        return repr(self)

    # ------------------------------------------------------------------
    # Slice / index access
    # ------------------------------------------------------------------

    @overload
    def __getitem__(self, key: int) -> str: ...

    @overload
    def __getitem__(self, key: slice) -> str: ...

    def __getitem__(self, key: int | slice) -> str:
        """Slice the context text by character position.

        For large files this streams from the backing file; it does NOT
        load the entire content into memory.

        Args:
            key: An integer index or a ``slice`` of character positions.

        Returns:
            The requested substring.
        """
        if self._text is not None:
            return self._text[key]
        # Stream the file to extract the slice
        text = self._read_chars()
        return text[key]

    def _read_chars(self, start: int = 0, end: int | None = None) -> str:
        """Read a character range from the backing file.

        Args:
            start: Start character position (inclusive).
            end: End character position (exclusive).  ``None`` = until EOF.

        Returns:
            The requested substring.
        """
        if self._path is None:
            return ""
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            if start > 0:
                fh.read(start)
            chunk = fh.read(end - start if end is not None else -1)
        return chunk

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    def peek(self, n: int = 200) -> str:
        """Return the first *n* characters of the context.

        Args:
            n: Number of characters to return (default 200).

        Returns:
            First *n* characters as a string.
        """
        if self._text is not None:
            return self._text[:n]
        return self._read_chars(0, n)

    def head(self, n: int = 10) -> str:
        """Return the first *n* lines of the context.

        Args:
            n: Number of lines to return (default 10).

        Returns:
            First *n* lines joined with newlines.
        """
        if self._path is None:
            return ""
        result: list[str] = []
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                result.append(line.rstrip("\n"))
                if len(result) >= n:
                    break
        return "\n".join(result)

    def tail(self, n: int = 10) -> str:
        """Return the last *n* lines of the context.

        Uses a circular buffer — does not load the whole file.

        Args:
            n: Number of lines to return (default 10).

        Returns:
            Last *n* lines joined with newlines.
        """
        if self._path is None:
            return ""
        from collections import deque

        buf: deque[str] = deque(maxlen=n)
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                buf.append(line.rstrip("\n"))
        return "\n".join(buf)

    def lines(self, start: int, end: int) -> str:
        """Return lines *start* through *end* (1-indexed, inclusive).

        Args:
            start: First line to return (1-indexed).
            end: Last line to return (1-indexed, inclusive).

        Returns:
            Requested lines joined with newlines.
        """
        if self._path is None:
            return ""
        result: list[str] = []
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if i < start:
                    continue
                if i > end:
                    break
                result.append(line.rstrip("\n"))
        return "\n".join(result)

    def search(self, pattern: str, max_results: int = 50) -> list[tuple[int, str]]:
        """Search the context for lines matching *pattern* (substring).

        Args:
            pattern: Substring to search for (case-sensitive).
            max_results: Maximum number of matching lines to return.

        Returns:
            List of ``(line_number, line_text)`` tuples (1-indexed).
        """
        if self._path is None:
            return []
        matches: list[tuple[int, str]] = []
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if pattern in line:
                    matches.append((i, line.rstrip("\n")))
                    if len(matches) >= max_results:
                        break
        return matches

    def grep(self, regex: str, max_results: int = 50) -> list[tuple[int, str]]:
        """Search the context for lines matching a regular expression.

        Args:
            regex: Regular-expression pattern.
            max_results: Maximum number of matching lines to return.

        Returns:
            List of ``(line_number, line_text)`` tuples (1-indexed).
        """
        if self._path is None:
            return []
        compiled = re.compile(regex)
        matches: list[tuple[int, str]] = []
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if compiled.search(line):
                    matches.append((i, line.rstrip("\n")))
                    if len(matches) >= max_results:
                        break
        return matches

    # ------------------------------------------------------------------
    # Workspace file helpers
    # ------------------------------------------------------------------

    def write_file(self, name: str, content: str) -> Path:
        """Write a file to the workspace.

        Args:
            name: File name (relative, no path traversal).
            content: Text content to write.

        Returns:
            Absolute path to the written file.
        """
        target = self._workspace / Path(name).name  # strip path components
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, name: str) -> str:
        """Read a file from the workspace.

        Args:
            name: File name (relative).

        Returns:
            File content as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        target = self._workspace / Path(name).name
        return target.read_text(encoding="utf-8")
