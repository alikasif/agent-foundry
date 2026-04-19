"""Unit tests for SkillCatalogEntry.mode field and _scan_dir parsing."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from skillful_agent.skill_manager import SkillCatalogEntry, SkillManager

_MINIMAL_FRONTMATTER = """\
---
name: {name}
description: A test skill.
{mode_line}---

# Body

Some skill content.
"""


def _write_skill_md(skill_dir: Path, name: str, mode_line: str = "") -> Path:
    """Write a minimal SKILL.md into *skill_dir/<name>/SKILL.md*."""
    subdir = skill_dir / name
    subdir.mkdir(parents=True, exist_ok=True)
    skill_md = subdir / "SKILL.md"
    skill_md.write_text(
        _MINIMAL_FRONTMATTER.format(name=name, mode_line=mode_line),
        encoding="utf-8",
    )
    return skill_md


# ---------------------------------------------------------------------------
# SkillCatalogEntry field tests
# ---------------------------------------------------------------------------


def test_catalog_entry_mode_defaults_to_inline() -> None:
    """SkillCatalogEntry constructed without mode kwarg must have mode='inline'."""
    entry = SkillCatalogEntry(
        name="test-skill",
        description="A test skill.",
        location=Path("/fake/SKILL.md"),
        skill_dir=Path("/fake"),
    )
    assert entry.mode == "inline"


# ---------------------------------------------------------------------------
# _scan_dir tests
# ---------------------------------------------------------------------------


def test_scan_dir_reads_mode_agent(tmp_path: Path) -> None:
    """_scan_dir must parse mode: agent from SKILL.md frontmatter."""
    _write_skill_md(tmp_path, "agent-skill", mode_line="mode: agent\n")
    manager = SkillManager()
    entries = manager._scan_dir(tmp_path)
    assert len(entries) == 1
    assert entries[0].mode == "agent"


def test_scan_dir_defaults_mode_when_absent(tmp_path: Path) -> None:
    """_scan_dir must default mode to 'inline' when frontmatter has no mode key."""
    _write_skill_md(tmp_path, "plain-skill")
    manager = SkillManager()
    entries = manager._scan_dir(tmp_path)
    assert len(entries) == 1
    assert entries[0].mode == "inline"


def test_scan_dir_warns_on_invalid_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_scan_dir must log a warning and default to 'inline' for unrecognized modes."""
    _write_skill_md(tmp_path, "turbo-skill", mode_line="mode: turbo\n")
    manager = SkillManager()
    with caplog.at_level(logging.WARNING, logger="skillful_agent.skill_manager"):
        entries = manager._scan_dir(tmp_path)
    assert len(entries) == 1
    assert entries[0].mode == "inline"
    assert any("turbo" in record.message for record in caplog.records), (
        "Expected a warning mentioning the invalid mode 'turbo'"
    )
