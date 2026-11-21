"""Read-only question-answering tools over the bundled ``repo/`` folder.

The folder ships inside the harness directory, so the same path works on the
host and at the read-only mount in the Docker sandbox.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1] / "repo"
MAX_HITS = 20


def _files() -> list[Path]:
    """Every regular file under ``REPO``, sorted by relative POSIX path.

    Sorting by the string (not the ``Path``) keeps the order the same on
    Windows, where ``Path`` comparison ignores case, and in the sandbox.
    """
    files = (p for p in REPO.rglob("*") if p.is_file())
    return sorted(files, key=lambda p: p.relative_to(REPO).as_posix())


def list_files() -> list[str]:
    """Relative POSIX paths of all files in the repository."""
    return [p.relative_to(REPO).as_posix() for p in _files()]


def search_repo(query: str, limit: int = 5) -> list[dict[str, str | int]]:
    """Lines containing ``query`` (case-insensitive), in path and line order.

    Each hit is ``{"path", "line", "text"}``; at most ``limit`` hits (<= 20).
    """
    needle = query.strip().lower()
    if not needle:
        raise ValueError("query must not be empty")
    hits: list[dict[str, str | int]] = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                rel = path.relative_to(REPO).as_posix()
                hits.append({"path": rel, "line": number, "text": line.strip()})
    return hits[: max(0, min(limit, MAX_HITS))]


def read_repo_file(path: str) -> str:
    """The text of one repository file, by its relative path."""
    target = (REPO / path).resolve()
    if not target.is_relative_to(REPO) or not target.is_file():
        raise ValueError(f"no such file in the repository: {path!r}")
    return target.read_text(encoding="utf-8")
