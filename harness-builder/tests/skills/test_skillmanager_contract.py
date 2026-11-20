"""T054: contract tests pinning the sibling ``SkillManager`` surface we rely on.

``ScopedSkillManager`` (T049) subclasses ``skillful_agent.SkillManager`` and
depends on private names (``_scan_dir``, ``_parse_skill_md``, ``_catalog``,
``_find_project_root``). If the sibling renames or changes them, these tests
fail first. They also pin the sibling's three reporting channels and its
parser quirks, so a reader knows exactly which problems are loud and which
are silent:

- name/dir mismatch: ``warnings.warn`` (skill still registered);
- shadowing: ``logger.warning`` only;
- skipped skill (missing name/description, unparsable YAML): ``logger.error``
  only, no warning;
- ``---`` inside a value cuts it (T052 finding), ``": "`` drops the skill and
  `` #`` truncates it silently (T054 findings).
"""

import inspect
import logging
import os
import subprocess
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from skillful_agent.skill_manager import SkillManager

from harness_builder.core.types import Message
from harness_builder.skills import store as store_mod
from harness_builder.skills import validation
from harness_builder.skills._compat import (
    new_scoped_manager,
    scoped_skill_manager_class,
)
from harness_builder.skills.extension import SkillsExtension
from harness_builder.skills.store import SkillStore

SIBLING_LOGGER = "skillful_agent.skill_manager"
COMPAT_LOGGER = "harness_builder.skills._compat"
BUNDLED = ("coding", "preflight", "task-reminder")

Writer = Callable[..., Path]


@dataclass
class Ctx:
    """A minimal ``RunContextView`` for calling the contributor directly."""

    run_id: str = "run-1"
    depth: int = 0
    workdir: Path | None = None
    messages: list[Message] = field(default_factory=list)
    ledger: Any = None
    recorder: Any = None


def discover(paths: list[Path]) -> tuple[list[Any], list[str]]:
    """Discover over ``paths``; return (entries, every warning message)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        entries = new_scoped_manager(paths).discover_skills()
    return entries, [str(w.message) for w in caught]


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """Messages of sibling/compat log records at exactly ``level``."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == level and r.name in (SIBLING_LOGGER, COMPAT_LOGGER)
    ]


def make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or an NTFS junction on Windows; else skip."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform != "win32":
        pytest.skip("cannot create a directory symlink here")
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("cannot create an NTFS junction here")


# --- private surface of the sibling ---------------------------------------


@pytest.mark.parametrize(
    ("attr", "params"),
    [
        ("_scan_dir", ["self", "skills_dir"]),
        ("_parse_skill_md", ["self", "path"]),
        ("_find_project_root", ["self"]),
        ("_find_entry", ["self", "name"]),
        ("discover_skills", ["self"]),
        ("load_skill_body", ["self", "name"]),
        ("list_skill_resources", ["self", "name"]),
    ],
)
def test_sibling_private_methods_keep_their_signatures(
    attr: str, params: list[str]
) -> None:
    method = getattr(SkillManager, attr)
    assert list(inspect.signature(method).parameters) == params


def test_catalog_and_body_cache_are_unset_until_used(tmp_path: Path) -> None:
    manager: Any = new_scoped_manager([tmp_path])
    assert manager._catalog is None
    assert manager._body_cache == {}


def test_discover_skills_honours_the_private_catalog_cache(
    tmp_path: Path, write_skill: Writer
) -> None:
    write_skill(tmp_path, "alpha")
    manager: Any = new_scoped_manager([tmp_path])
    found = manager.discover_skills()
    assert manager._catalog is found
    write_skill(tmp_path, "beta")
    assert [e.name for e in manager.discover_skills()] == ["alpha"]
    manager._catalog = None
    assert sorted(e.name for e in manager.discover_skills()) == ["alpha", "beta"]


def test_scoped_class_overrides_only_discovery_and_root() -> None:
    scoped = scoped_skill_manager_class()
    assert issubclass(scoped, SkillManager)
    assert scoped is scoped_skill_manager_class()
    assert scoped is store_mod.ScopedSkillManager
    own = set(vars(scoped))
    assert {"__init__", "discover_skills", "_find_project_root"} <= own
    for inherited in (
        "_scan_dir",
        "_parse_skill_md",
        "_find_entry",
        "load_skill_body",
        "list_skill_resources",
        "available_names",
        "build_catalog_text",
    ):
        assert inherited not in own
        assert getattr(scoped, inherited) is getattr(SkillManager, inherited)


def test_find_project_root_is_neutral_and_never_scanned(tmp_path: Path) -> None:
    live = tmp_path / "proj" / ".agents" / "skills"
    manager: Any = new_scoped_manager([live])
    assert manager._find_project_root() == live.resolve().parent.parent
    empty: Any = new_scoped_manager([])
    assert empty._find_project_root() == Path.cwd()
    assert empty.discover_skills() == []


# --- _scan_dir contract ------------------------------------------------------


def test_scan_dir_entries_are_sorted_resolved_and_filtered(
    tmp_path: Path, write_skill: Writer
) -> None:
    write_skill(tmp_path, "zeta")
    write_skill(tmp_path, "alpha")
    (tmp_path / "loose-file.md").write_bytes(b"not a skill dir")
    (tmp_path / "no-skill-md").mkdir()
    (tmp_path / "no-skill-md" / "README.md").write_bytes(b"x")
    manager: Any = new_scoped_manager([tmp_path])
    entries = manager._scan_dir(tmp_path)
    assert [e.name for e in entries] == ["alpha", "zeta"]
    for entry in entries:
        assert entry.skill_dir == (tmp_path / entry.name).resolve()
        assert entry.location == (tmp_path / entry.name / "SKILL.md").resolve()
        assert entry.mode == "inline"


def test_scan_dir_mode_agent_kept_and_unknown_mode_logged(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    write_skill(
        tmp_path, "agentic", frontmatter="name: agentic\ndescription: d\nmode: Agent\n"
    )
    write_skill(tmp_path, "odd", frontmatter="name: odd\ndescription: d\nmode: x\n")
    caplog.set_level(logging.WARNING)
    entries, caught = discover([tmp_path])
    modes = {e.name: e.mode for e in entries}
    assert modes == {"agentic": "agent", "odd": "inline"}
    assert caught == []
    assert any("unrecognized mode" in m for m in records(caplog, logging.WARNING))


# --- the three reporting channels -------------------------------------------


def test_name_dir_mismatch_is_a_warning_and_still_registered(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    write_skill(tmp_path, "real-name", dir_name="other-dir")
    caplog.set_level(logging.WARNING)
    entries, caught = discover([tmp_path])
    assert [e.name for e in entries] == ["real-name"]
    assert entries[0].skill_dir.name == "other-dir"
    assert caught == ["Skill name 'real-name' does not match directory 'other-dir'"]
    assert records(caplog, logging.ERROR) == []


def test_store_skill_dir_does_not_follow_a_mismatched_name(
    tmp_path: Path, write_skill: Writer
) -> None:
    store = SkillStore(tmp_path)
    write_skill(store.live, "real-name", dir_name="other-dir")
    with pytest.warns(UserWarning, match="does not match directory"):
        entries = store.catalog_entries()
    assert [e.name for e in entries] == ["real-name"]
    assert entries[0].path is not None
    assert Path(entries[0].path).parent.name == "other-dir"
    assert not store.skill_dir("real-name").exists()


@pytest.mark.parametrize(
    ("frontmatter", "needle"),
    [
        ("name: no-desc\n", "'description' is required"),
        ("description: has no name\n", "'name' is required"),
        ("name: [unclosed\ndescription: d\n", "Cannot parse YAML"),
    ],
)
def test_skipped_skill_is_an_error_log_only(
    tmp_path: Path,
    write_skill: Writer,
    caplog: pytest.LogCaptureFixture,
    frontmatter: str,
    needle: str,
) -> None:
    write_skill(tmp_path, "broken", frontmatter=frontmatter)
    caplog.set_level(logging.WARNING)
    entries, caught = discover([tmp_path])
    assert entries == []
    assert caught == []
    errors = records(caplog, logging.ERROR)
    assert len(errors) == 1 and needle in errors[0]


def test_shadowing_is_a_log_warning_only(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    low, high = tmp_path / "low", tmp_path / "high"
    write_skill(low, "same", "Low priority copy.")
    write_skill(high, "same", "High priority copy.")
    caplog.set_level(logging.WARNING)
    entries, caught = discover([low, high])
    assert [(e.name, e.description) for e in entries] == [
        ("same", "High priority copy.")
    ]
    assert caught == []
    assert any("shadowed" in m for m in records(caplog, logging.WARNING))


# --- parser quirks: lenient sibling vs strict validator ----------------------


def test_triple_dash_in_description_is_cut_by_sibling_and_rejected_strictly(
    tmp_path: Path, write_skill: Writer
) -> None:
    skill = write_skill(tmp_path, "dashy", "keep a --- lose this part")
    entries, caught = discover([tmp_path])
    assert [(e.name, e.description) for e in entries] == [("dashy", "keep a")]
    assert caught == []
    manager: Any = new_scoped_manager([tmp_path])
    catalog, _ = manager._parse_skill_md(skill / "SKILL.md")
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert validation._frontmatter_problems("dashy", text, catalog) == [
        "frontmatter contains '---'"
    ]


def test_colon_space_in_description_drops_the_skill_with_only_an_error_log(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    write_skill(tmp_path, "unit-convert", "Convert units: metres to feet")
    caplog.set_level(logging.WARNING)
    entries, caught = discover([tmp_path])
    assert entries == []
    assert caught == []  # a zero-warning check alone would pass
    assert any("Cannot parse YAML" in m for m in records(caplog, logging.ERROR))
    problems, _, _ = validation._manager_check(tmp_path, "unit-convert")
    assert problems == ["SkillManager discovered [], expected ['unit-convert']"]


def test_space_hash_in_description_is_truncated_silently_by_both_parsers(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    skill = write_skill(tmp_path, "word-tally", "Count words #fast in a text")
    caplog.set_level(logging.WARNING)
    entries, caught = discover([tmp_path])
    assert [(e.name, e.description) for e in entries] == [("word-tally", "Count words")]
    assert caught == [] and records(caplog, logging.ERROR) == []
    manager: Any = new_scoped_manager([tmp_path])
    catalog, _ = manager._parse_skill_md(skill / "SKILL.md")
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    # Known gap: the strict check agrees with the truncated parse.
    assert validation._frontmatter_problems("word-tally", text, catalog) == []


def test_parse_without_frontmatter_returns_empty_mapping(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"\n  just a body, no fences  \n")
    manager: Any = new_scoped_manager([tmp_path])
    assert manager._parse_skill_md(path) == ({}, "just a body, no fences")


def test_parse_non_mapping_yaml_gives_empty_mapping(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\n- a\n- b\n---\nbody\n")
    manager: Any = new_scoped_manager([tmp_path])
    assert manager._parse_skill_md(path) == ({}, "body")


def test_horizontal_rule_in_body_survives_parsing(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\nname: x\ndescription: d\n---\nintro\n\n---\n\noutro\n")
    manager: Any = new_scoped_manager([tmp_path])
    frontmatter, body = manager._parse_skill_md(path)
    assert frontmatter == {"name": "x", "description": "d"}
    assert body == "intro\n\n---\n\noutro"


# --- scoped discovery: only explicit paths -----------------------------------


@pytest.mark.anyio
async def test_contributor_never_offers_bundled_user_or_cwd_project_skills(
    tmp_path: Path,
    write_skill: Writer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    write_skill(home / ".agents" / "skills", "home-only", "Personal helper skill.")
    project = tmp_path / "cwd-project"
    (project / "pyproject.toml").parent.mkdir(parents=True)
    (project / "pyproject.toml").write_bytes(b"[project]\nname='x'\n")
    write_skill(project / ".agents" / "skills", "cwd-only", "Project helper skill.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    store = SkillStore(tmp_path / "lib")
    write_skill(store.live, "helper-tool", "Generic helper skill.")
    task = (
        "Use coding, preflight, task-reminder, home-only and cwd-only; "
        "any helper skill will do."
    )
    ext = SkillsExtension(store, top_k=10)
    text = await ext.contribute(task, Ctx())
    assert ext.offered("run-1") == ["helper-tool"]
    assert text is not None
    for name in (*BUNDLED, "home-only", "cwd-only"):
        assert f"<name>{name}</name>" not in text

    opted = SkillsExtension(SkillStore(store.root, include_user_skills=True), top_k=10)
    await opted.contribute(task, Ctx(run_id="run-2"))
    assert sorted(opted.offered("run-2")) == ["helper-tool", "home-only"]


def test_bundled_skills_exist_in_the_sibling_but_not_in_any_store(
    tmp_path: Path,
) -> None:
    bundled = Path(inspect.getfile(SkillManager)).parent / "skills"
    assert {p.name for p in bundled.iterdir() if p.is_dir()} >= set(BUNDLED)
    store = SkillStore(tmp_path)
    assert store.catalog_entries() == []
    assert bundled.resolve() not in [p.resolve() for p in store.search_paths()]
    assert store.manager().search_paths == [store.live.resolve()]


def test_link_to_a_prefix_sibling_directory_is_dropped(
    tmp_path: Path, write_skill: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    live = tmp_path / "skills"
    live.mkdir()
    write_skill(tmp_path / "skills-evil", "escaper")
    make_dir_link(live / "escaper", tmp_path / "skills-evil" / "escaper")
    write_skill(live, "inside")
    caplog.set_level(logging.WARNING)
    entries, _ = discover([live])
    assert [e.name for e in entries] == ["inside"]
    assert any("resolves outside" in m for m in records(caplog, logging.WARNING))


# --- the committed fixture library ------------------------------------------


def test_fixture_library_loads_without_warnings_or_logs(
    library_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entries, caught = discover([library_dir])
    assert caught == []
    assert records(caplog, logging.WARNING) == []
    assert records(caplog, logging.ERROR) == []
    dirs = sorted(p.name for p in library_dir.iterdir() if p.is_dir())
    assert len(dirs) == 36
    assert sorted(e.name for e in entries) == dirs


def test_every_fixture_passes_the_strict_frontmatter_check(library_dir: Path) -> None:
    manager: Any = new_scoped_manager([library_dir])
    problems: dict[str, list[str]] = {}
    for entry in manager.discover_skills():
        catalog, body = manager._parse_skill_md(entry.location)
        text = entry.location.read_text(encoding="utf-8")
        found = validation._frontmatter_problems(entry.name, text, catalog)
        if found:
            problems[entry.name] = found
        assert f"FIXTURE-BODY-{entry.name}" in body
    assert problems == {}
