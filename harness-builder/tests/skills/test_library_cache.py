"""T059 (T058 MA-2): the per-run catalog and index are cached on a fingerprint."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from harness_builder.skills.extension import SkillsExtension
from harness_builder.skills.store import SkillStore

from .support import write_live_skill  # pyrefly: ignore[missing-import]


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """A library with two skills."""
    store = SkillStore(tmp_path)
    write_live_skill(store, "pdf-merge", "Merge PDF files into one document.")
    write_live_skill(store, "csv-clean", "Clean and normalise CSV columns.")
    return store


def names(ext: SkillsExtension, task: str) -> list[str]:
    """Offered names for ``task``."""
    _, chosen = ext._select(task)
    return [entry.name for entry in chosen]


def test_unchanged_library_reuses_manager_and_index(store: SkillStore) -> None:
    ext = SkillsExtension(store)
    first = ext._library()
    assert ext._library()[0] is first[0] and ext._library()[2] is first[2]


def test_newly_approved_skill_is_found_by_the_next_run(store: SkillStore) -> None:
    ext = SkillsExtension(store)
    assert "zebra-count" not in names(ext, "count zebra stripes")
    write_live_skill(store, "zebra-count", "Count the stripes on a zebra photograph.")
    assert names(ext, "count zebra stripes")[0] == "zebra-count"


def test_edited_and_removed_skills_invalidate_the_cache(store: SkillStore) -> None:
    ext = SkillsExtension(store)
    before = ext._library()[1]
    write_live_skill(store, "pdf-merge", "Rotate and watermark PDF pages instead.")
    after = ext._library()[1]
    assert {e.name: e.description for e in after}["pdf-merge"].startswith("Rotate")
    assert before is not after
    shutil.rmtree(store.live / "csv-clean")
    assert [e.name for e in ext._library()[1]] == ["pdf-merge"]


def test_frontmatter_is_parsed_once_per_skill(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager: Any = store.manager()
    parsed: list[Path] = []
    original = type(manager)._parse_skill_md

    def counting(self: object, path: Path) -> object:
        parsed.append(path)
        return original(self, path)  # pyrefly: ignore[bad-argument-type]

    monkeypatch.setattr(type(manager), "_parse_skill_md", counting)
    entries = SkillsExtension(store)._library()[1]
    assert len(entries) == 2
    assert len(parsed) == 2  # discovery only; triggers reuse the remembered parse
