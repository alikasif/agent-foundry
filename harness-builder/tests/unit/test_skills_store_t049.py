"""T049 unit tests: ScopedSkillManager scoping, SkillStore paths, root, stats.

T054 owns the sibling contract suite (``tests/skills``); these tests cover the
T049 code only. The first real-manager test pays the cold skillful_agent
import (about 16 s), once per session.
"""

import json
import os
import subprocess
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness_builder.errors import ConfigError
from harness_builder.skills import _compat
from harness_builder.skills.store import (
    SkillStats,
    SkillStore,
    StatsCorruptError,
    resolve_skills_root,
    validate_skill_name,
)

pytestmark = pytest.mark.timeout(180)

BUNDLED = {"coding", "preflight", "task-reminder"}


def write_skill(
    skills_dir: Path,
    name: str,
    description: str = "Does a thing.",
    *,
    dir_name: str | None = None,
    extra: str = "",
) -> Path:
    """Write ``<skills_dir>/<dir_name or name>/SKILL.md`` and return its dir."""
    skill = skills_dir / (dir_name or name)
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n"
        f"Body of {name}.\n",
        encoding="utf-8",
    )
    return skill


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``Path.home()`` points at a tmp dir holding a user-level skill."""
    home = tmp_path / "home"
    write_skill(home / ".agents" / "skills", "user-skill", "User level skill.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# --- catalog scoping ---------------------------------------------------------


def test_catalog_has_only_explicit_path_skills(
    tmp_path: Path, isolated_home: Path
) -> None:
    store = SkillStore(tmp_path / "proj")
    write_skill(store.live, "csv-summary", "Summarize a CSV file.")
    write_skill(store.pending / "c1", "pending-skill")

    names = {e.name for e in store.catalog_entries()}

    assert names == {"csv-summary"}
    assert not names & BUNDLED
    assert "user-skill" not in names


def test_user_skills_only_when_opted_in(tmp_path: Path, isolated_home: Path) -> None:
    store = SkillStore(tmp_path / "proj", include_user_skills=True)
    write_skill(store.live, "csv-summary")

    assert store.search_paths()[-1] == store.live
    assert {e.name for e in store.catalog_entries()} == {"csv-summary", "user-skill"}


def test_later_path_shadows_earlier(tmp_path: Path, isolated_home: Path) -> None:
    store = SkillStore(tmp_path / "proj", include_user_skills=True)
    write_skill(store.live, "user-skill", "Project override.")

    (entry,) = store.catalog_entries()

    assert entry.description == "Project override."


def test_manager_is_a_skill_manager_subclass_and_neutral_root(tmp_path: Path) -> None:
    from skillful_agent.skill_manager import SkillManager

    store = SkillStore(tmp_path / "proj")
    manager = store.manager()

    assert isinstance(manager, SkillManager)
    assert manager.search_paths == [store.live]
    assert manager._project_root == store.root  # pyrefly: ignore[missing-attribute]
    assert _compat.scoped_skill_manager_class() is type(manager)


def test_missing_live_dir_gives_empty_catalog(tmp_path: Path) -> None:
    assert SkillStore(tmp_path).catalog_entries() == []


def test_new_manager_sees_promoted_skill_but_old_one_is_cached(
    tmp_path: Path,
) -> None:
    store = SkillStore(tmp_path)
    old = store.manager()
    assert old.available_names() == []
    write_skill(store.live, "new-skill")

    assert old.available_names() == []
    assert store.manager().available_names() == ["new-skill"]


def test_inherited_body_and_resources(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    skill = write_skill(store.live, "csv-summary")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "csv_summary.py").write_text("print(1)\n", encoding="utf-8")
    manager = store.manager()

    assert manager.load_skill_body("csv-summary") == "Body of csv-summary."
    assert manager.list_skill_resources("csv-summary") == ["scripts/csv_summary.py"]


def test_name_dir_mismatch_still_warns(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    write_skill(store.live, "other-name", dir_name="dir-name")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.manager().discover_skills()

    assert any("does not match directory" in str(w.message) for w in caught)


def test_clean_skill_loads_without_warnings(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    write_skill(store.live, "csv-summary")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.manager().discover_skills()

    assert caught == []


def _link_dir(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform != "win32":
        pytest.skip("cannot create a directory symlink here")
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip("cannot create a symlink or junction here")


def test_linked_skill_leaving_the_search_path_is_dropped(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "proj")
    store.live.mkdir(parents=True)
    outside = write_skill(tmp_path / "outside", "escaper")
    _link_dir(store.live / "escaper", outside)
    write_skill(store.live, "inside-skill")

    assert [e.name for e in store.catalog_entries()] == ["inside-skill"]


def test_symlinked_skill_md_leaving_the_skill_dir_is_dropped(
    tmp_path: Path,
) -> None:
    store = SkillStore(tmp_path / "proj")
    outside = write_skill(tmp_path / "outside", "escaper") / "SKILL.md"
    (store.live / "escaper").mkdir(parents=True)
    try:
        os.symlink(outside, store.live / "escaper" / "SKILL.md")
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create file symlinks here: {exc}")
    write_skill(store.live, "inside-skill")

    assert [e.name for e in store.catalog_entries()] == ["inside-skill"]


# --- SkillEntry mapping ------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("", []),
        ("triggers: summarize csv\n", ["summarize csv"]),
        ("triggers:\n  - csv\n  - table stats\n", ["csv", "table stats"]),
        ("triggers: 3\n", []),
    ],
)
def test_catalog_entries_map_triggers_and_path(
    tmp_path: Path, extra: str, expected: list[str]
) -> None:
    store = SkillStore(tmp_path)
    skill = write_skill(store.live, "csv-summary", "Summarize a CSV.", extra=extra)

    (entry,) = store.catalog_entries()

    assert entry.name == "csv-summary"
    assert entry.description == "Summarize a CSV."
    assert entry.triggers == expected
    assert entry.path == str((skill / "SKILL.md").resolve())


# --- paths -------------------------------------------------------------------


def test_layout_paths(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.ensure_dirs()
    agents = tmp_path.resolve() / ".agents"

    assert store.live == agents / "skills"
    assert store.pending == agents / "skills-pending"
    assert store.stats_path == agents / "skills-meta" / "stats.json"
    assert store.trash == agents / "skills-trash"
    assert all(p.is_dir() for p in (store.live, store.pending, store.meta))
    assert store.trash.is_dir()
    assert store.pending not in store.search_paths()


@pytest.mark.parametrize(
    "name", ["../x", "a/b", "a\\b", "..", "", "Upper", "a--b", "-a", "a_b", "a b"]
)
def test_invalid_skill_names_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError):
        validate_skill_name(name)
    with pytest.raises(ConfigError):
        SkillStore(tmp_path).skill_dir(name)


def test_valid_name_paths(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    now = datetime(2026, 11, 19, 1, 2, 3, 4, tzinfo=UTC)

    assert store.skill_dir("csv-summary-2") == store.live / "csv-summary-2"
    assert store.candidate_dir("c_1-a") == store.pending / "c_1-a"
    assert store.trash_dir("csv", now) == store.trash / "csv-20261119T010203000004Z"


@pytest.mark.parametrize("cid", ["..", "../x", "a/b", "", ".hidden", "a\\b"])
def test_invalid_candidate_ids_rejected(tmp_path: Path, cid: str) -> None:
    with pytest.raises(ConfigError):
        SkillStore(tmp_path).candidate_dir(cid)


# --- root resolution (U4) ----------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """``proj/pyproject.toml`` with a nested ``proj/a/b`` working dir."""
    (tmp_path / "proj" / "a" / "b").mkdir(parents=True)
    (tmp_path / "proj" / "pyproject.toml").write_text("", encoding="utf-8")
    return tmp_path


def test_root_order_config_first(tree: Path) -> None:
    cwd = tree / "proj" / "a" / "b"
    env = {"HARNESS_SKILLS_ROOT": str(tree / "env")}

    got = resolve_skills_root(tree / "cfg", tree / "cli", environ=env, cwd=cwd)

    assert got == (tree / "cfg").resolve()


def test_root_order_cli_then_env(tree: Path) -> None:
    cwd = tree / "proj" / "a" / "b"
    env = {"HARNESS_SKILLS_ROOT": str(tree / "env")}

    assert (
        resolve_skills_root(None, tree / "cli", environ=env, cwd=cwd)
        == (tree / "cli").resolve()
    )
    assert resolve_skills_root(environ=env, cwd=cwd) == (tree / "env").resolve()


def test_root_falls_back_to_pyproject_ancestor_then_cwd(tree: Path) -> None:
    cwd = tree / "proj" / "a" / "b"

    assert resolve_skills_root(environ={}, cwd=cwd) == (tree / "proj").resolve()
    assert (
        resolve_skills_root(environ={"HARNESS_SKILLS_ROOT": ""}, cwd=cwd)
        == (tree / "proj").resolve()
    )


def test_root_is_cwd_without_pyproject(tmp_path: Path) -> None:
    if any((p / "pyproject.toml").is_file() for p in tmp_path.resolve().parents):
        pytest.skip("a pyproject.toml exists above the tmp dir")
    assert resolve_skills_root(environ={}, cwd=tmp_path) == tmp_path.resolve()


def test_relative_roots_are_cwd_relative(tree: Path) -> None:
    cwd = tree / "proj"

    assert (
        resolve_skills_root(Path("lib"), environ={}, cwd=cwd) == (cwd / "lib").resolve()
    )


# --- stats sidecar -----------------------------------------------------------


def test_record_use_persists_counts(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    t1 = datetime(2026, 11, 19, tzinfo=UTC)

    store.record_use("csv-summary", success=True, now=t1)
    got = store.record_use("csv-summary", success=False, now=t1)

    assert got == SkillStats(usage_count=2, success_count=1, last_used=t1)
    assert store.read_stats() == {"csv-summary": got}
    raw = json.loads(store.stats_path.read_text(encoding="utf-8"))
    assert raw["csv-summary"]["usage_count"] == 2


@pytest.mark.parametrize(
    "content",
    [b"", b"{not json", b'{"x": {"usage_count": "a"}}', b"\xff\xfe{"],
)
def test_corrupt_stats_raise(tmp_path: Path, content: bytes) -> None:
    store = SkillStore(tmp_path)
    store.meta.mkdir(parents=True)
    store.stats_path.write_bytes(content)

    with pytest.raises(StatsCorruptError):
        store.read_stats()


def test_missing_stats_read_as_empty(tmp_path: Path) -> None:
    assert SkillStore(tmp_path).read_stats() == {}


def test_failed_replace_keeps_old_stats_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SkillStore(tmp_path)
    store.record_use("a", success=True)
    before = store.stats_path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.record_use("a", success=True)

    assert store.stats_path.read_bytes() == before
    assert sorted(p.name for p in store.meta.iterdir()) == [
        "stats.json",
        "stats.lock",
    ]


def test_failed_write_before_replace_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SkillStore(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("cannot serialize")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(ValueError, match="cannot serialize"):
        store.record_use("a", success=True)

    assert [p.name for p in store.meta.iterdir()] == ["stats.lock"]


# --- lazy import -------------------------------------------------------------


def test_store_import_does_not_load_sibling_or_adk() -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import harness_builder.skills.store as s\n"
        "s.SkillStore(Path('.')).stats_path\n"
        "s.resolve_skills_root(environ={})\n"
        "bad = sorted(m for m in sys.modules"
        " if m.startswith(('skillful_agent', 'google.adk')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == ""


def test_unknown_module_attribute_raises() -> None:
    import harness_builder.skills.store as store_module

    with pytest.raises(AttributeError):
        _ = store_module.NoSuchThing  # pyrefly: ignore[missing-attribute]


def test_lazy_scoped_skill_manager_export() -> None:
    from harness_builder.skills.store import ScopedSkillManager

    assert ScopedSkillManager is _compat.scoped_skill_manager_class()
