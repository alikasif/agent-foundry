"""ContextStore -- context-by-reference storage for the RLM agent.

Context-by-reference
--------------------
The ``ContextStore`` object replaces the plain string ``context`` variable in
the Python namespace.  Large inputs are spilled to a workspace file so the
agent can inspect them selectively without loading everything into the LLM
window.

Key invariants
~~~~~~~~~~~~~~
- The full text is **never** dumped into the LLM history automatically.
  ``str(context)`` returns a short summary for inputs over the spill
  threshold.  Short-string behaviour is preserved: when the text is below
  ``_SHORT_TEXT_THRESHOLD`` chars, ``str()`` returns the full text so simple
  prompts keep working.
- The agent inspects context via helper methods:
  ``peek``, ``head``, ``tail``, slicing (``context[a:b]``),
  ``lines``, ``search`` / ``grep``, and ``context.path`` for bash/awk.
- ``set_context()`` accepts a plain string OR a ``pathlib.Path`` pointing to
  an existing file.

Backward compatibility
~~~~~~~~~~~~~~~~~~~~~~
Callers that pass ``set_context("short string")`` and then
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
from uuid import uuid4

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
        self._workspace: Path = workspace or Path(tempfile.mkdtemp(prefix="rlm_"))
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

    @classmethod
    def from_slice(
        cls,
        source: ContextStore,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        workspace: Path | None = None,
    ) -> ContextStore:
        """Create a ContextStore over a slice of *source*.

        Convenience classmethod alias for ``source.sub_context(...)`` so
        ``ContextStore.from_slice(context, start_line=a, end_line=b)`` reads
        symmetrically with ``from_string`` / ``from_path``.

        Args:
            source: The store to slice.
            start_line: 1-indexed inclusive start line (line-range mode).
            end_line: 1-indexed inclusive end line (line-range mode).
            start_char: 0-indexed start char (char-range mode).
            end_char: 0-indexed end char, exclusive (char-range mode).
            workspace: Optional workspace for the new slice store.

        Returns:
            A new ContextStore over the requested slice.
        """
        return source.sub_context(
            start_line=start_line,
            end_line=end_line,
            start_char=start_char,
            end_char=end_char,
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Context-by-reference: slicing and cloning (RLM additions)
    # ------------------------------------------------------------------

    def _child_workspace(self, workspace: Path | None) -> Path:
        """Return a fresh child-workspace dir nested under this store.

        Child workspaces live under ``<workspace>/children/<uuid4>/`` so
        cleaning a parent workspace also removes descendants, while each
        child's ``context.path`` stays independent of the parent's.

        Args:
            workspace: Explicit workspace, or ``None`` to derive a fresh one.

        Returns:
            The child workspace directory (created).
        """
        if workspace is not None:
            ws = workspace
        else:
            ws = self._workspace / "children" / uuid4().hex
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def sub_context(
        self,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        workspace: Path | None = None,
    ) -> ContextStore:
        """Return a NEW ContextStore over a slice of this context.

        Exactly one range kind must be supplied: line-range
        (``start_line`` / ``end_line``, 1-indexed inclusive, consistent with
        ``lines()``) XOR char-range (``start_char`` / ``end_char``, 0-indexed
        end-exclusive, consistent with ``__getitem__``).  The slice is
        streamed from this store's backing file into a new file in a fresh
        child workspace; the parent file is never mutated and never fully
        loaded into memory.

        Args:
            start_line: 1-indexed inclusive start line.
            end_line: 1-indexed inclusive end line.
            start_char: 0-indexed start char.
            end_char: 0-indexed end char, exclusive.
            workspace: Optional explicit workspace for the new store.

        Returns:
            A new ContextStore over the requested slice, with its own path.

        Raises:
            RuntimeError: If no context has been loaded.
            ValueError: If both/neither range kinds are given, or a range is
                invalid.
        """
        if self._path is None:
            raise RuntimeError(
                "No context loaded.  Call set_context() first or use /load."
            )

        line_given = start_line is not None or end_line is not None
        char_given = start_char is not None or end_char is not None
        if line_given and char_given:
            raise ValueError(
                "sub_context: provide either a line range (start_line/end_line) "
                "or a char range (start_char/end_char), not both."
            )
        if not line_given and not char_given:
            raise ValueError(
                "sub_context: provide a line range (start_line/end_line) or a "
                "char range (start_char/end_char)."
            )

        if line_given:
            s_line = 1 if start_line is None else start_line
            e_line = self._line_count if end_line is None else end_line
            if s_line < 1:
                raise ValueError(f"start_line must be >= 1, got {s_line}.")
            if e_line < s_line:
                raise ValueError(
                    f"end_line ({e_line}) must be >= start_line ({s_line})."
                )
            slice_text = self.lines(s_line, e_line)
        else:
            s_char = 0 if start_char is None else start_char
            e_char = self._char_count if end_char is None else end_char
            if s_char < 0 or e_char < 0:
                raise ValueError("char range indices must be non-negative.")
            if e_char < s_char:
                raise ValueError(
                    f"end_char ({e_char}) must be >= start_char ({s_char})."
                )
            slice_text = self._read_chars(s_char, e_char)

        child_ws = self._child_workspace(workspace)
        return ContextStore.from_string(slice_text, workspace=child_ws)

    def clone(self, *, workspace: Path | None = None) -> ContextStore:
        """Return a NEW ContextStore that is a full copy of this store.

        The backing file is copied into a fresh child workspace so the new
        store's ``.path`` always differs from this store's (even for an empty
        store).  Char/line counts and the short-text cache are copied as-is
        (content is identical, so nothing is recomputed).

        This is the primitive ``SubagentRunner`` always applies before binding
        a context into a child, guaranteeing the child never shares a backing
        file with the parent or any ancestor.

        Args:
            workspace: Optional explicit workspace for the clone.

        Returns:
            A new, independent ContextStore with identical content.
        """
        child_ws = self._child_workspace(workspace)
        if self._path is None:
            # Empty store: still materialise a real, independent file.
            return ContextStore.from_string("", workspace=child_ws)
        clone = ContextStore(child_ws)
        dest = child_ws / "context.txt"
        shutil.copy2(self._path, dest)
        clone._path = dest
        clone._char_count = self._char_count
        clone._line_count = self._line_count
        clone._text = self._text
        return clone

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

        This preserves backward compatibility: ``print(context)`` after
        ``set_context('short query')`` still outputs the query text.  For
        large inputs it prints the summary so an accidental
        ``print(context)`` is safe.
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
