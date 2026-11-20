"""Shared fixtures for the T054 skills tests (offline; no Docker, no network)."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from harness_builder.skills.store import SkillStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LIBRARY_DIR = FIXTURES_DIR / "library"
QUERIES_FILE = FIXTURES_DIR / "queries.json"

SkillWriter = Callable[..., Path]


@pytest.fixture
def library_dir() -> Path:
    """The committed fixture library (36 hand-written skills, read-only)."""
    return LIBRARY_DIR


@pytest.fixture
def labelled_queries() -> list[tuple[str, str]]:
    """``(query, gold skill name)`` pairs from ``fixtures/queries.json``."""
    data = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    return [(item["q"], item["gold"]) for item in data["queries"]]


@pytest.fixture
def fixture_store(tmp_path: Path) -> SkillStore:
    """A ``SkillStore`` in ``tmp_path`` whose live dir is a copy of the library."""
    store = SkillStore(tmp_path / "root")
    shutil.copytree(LIBRARY_DIR, store.live)
    return store


@pytest.fixture
def write_skill() -> SkillWriter:
    """Factory writing ``<parent>/<dir_name>/SKILL.md`` plus optional resources.

    Call as ``write_skill(parent, name, description, dir_name=None,
    frontmatter=None, body=..., scripts=None, references=None)``. ``frontmatter``
    replaces the generated YAML block verbatim. Files are written as bytes so
    Windows never adds CRLF.
    """

    def _write(
        parent: Path,
        name: str,
        description: str = "A fixture skill.",
        *,
        dir_name: str | None = None,
        frontmatter: str | None = None,
        body: str = "Fixture body.",
        scripts: dict[str, str] | None = None,
        references: dict[str, str] | None = None,
    ) -> Path:
        skill = parent / (dir_name or name)
        skill.mkdir(parents=True, exist_ok=True)
        block = frontmatter or f"name: {name}\ndescription: {description}\n"
        text = f"---\n{block}---\n\n{body}\n"
        (skill / "SKILL.md").write_bytes(text.encode("utf-8"))
        for sub, files in (("scripts", scripts), ("references", references)):
            for file, content in (files or {}).items():
                (skill / sub).mkdir(exist_ok=True)
                (skill / sub / file).write_bytes(content.encode("utf-8"))
        return skill

    return _write
