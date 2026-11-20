"""T054: SkillStore layout, explicit search paths and the stats sidecar.

Complements ``tests/unit/test_skills_store_t049.py``: here the layout dirs
other than ``live`` are shown never to be discovered, counters never touch
SKILL.md, the documented corrupt-file tradeoffs are pinned, and concurrent
writers are exercised.
"""

import hashlib
import json
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from harness_builder.skills.extension import SkillsExtension, install_skills
from harness_builder.skills.store import (
    SKILLS_ROOT_ENV,
    SkillStats,
    SkillStore,
    resolve_skills_root,
)
from harness_builder.spec.models import SkillsConfig

Writer = Callable[..., Path]


class RecordingBuilder:
    """Captures what ``install_skills`` registers (a ``SkillsBuilder``)."""

    def __init__(self) -> None:
        self.contributors: list[object] = []
        self.tool_names: list[str] = []
        self.hooks: list[tuple[str, int, str | None]] = []

    def tools(self, *tools: object) -> object:
        self.tool_names += [str(getattr(t, "name", "")) for t in tools]
        return self

    def hook(
        self,
        event: str,
        fn: Callable[..., object],
        priority: int = 0,
        name: str | None = None,
    ) -> object:
        self.hooks.append((event, priority, name))
        return self

    def prompt_contributor(self, contributor: object) -> object:
        self.contributors.append(contributor)
        return self


def tree_hash(directory: Path) -> str:
    """SHA-256 over relative paths and bytes of every file under ``directory``."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --- layout ------------------------------------------------------------------


def test_ensure_dirs_creates_the_four_layout_dirs(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.ensure_dirs()
    agents = tmp_path.resolve() / ".agents"
    assert sorted(p.name for p in agents.iterdir()) == [
        "skills",
        "skills-meta",
        "skills-pending",
        "skills-trash",
    ]
    store.ensure_dirs()  # idempotent


def test_only_live_is_discovered_never_pending_meta_or_trash(
    tmp_path: Path, write_skill: Writer
) -> None:
    store = SkillStore(tmp_path)
    store.ensure_dirs()
    write_skill(store.live, "live-one")
    write_skill(store.candidate_dir("20261120T000000Z-abcd1234"), "pending-one")
    write_skill(store.pending, "pending-top")
    write_skill(store.meta, "meta-one")
    write_skill(store.trash, "trashed-one", dir_name="trashed-one-20261120T0Z")
    write_skill(store.agents_dir, "agents-root-one")
    assert [e.name for e in store.catalog_entries()] == ["live-one"]
    assert store.search_paths() == [store.live]


def test_user_skills_are_lowest_priority_when_opted_in(
    tmp_path: Path, write_skill: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_skill(home / ".agents" / "skills", "shared", "User copy.")
    store = SkillStore(tmp_path / "root", include_user_skills=True)
    write_skill(store.live, "shared", "Project copy.")
    assert store.search_paths() == [home / ".agents" / "skills", store.live]
    assert [(e.name, e.description) for e in store.catalog_entries()] == [
        ("shared", "Project copy.")
    ]


def test_root_resolution_order_table(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_bytes(b"")
    env = {SKILLS_ROOT_ENV: str(tmp_path / "env")}
    cfg, cli = tmp_path / "cfg", tmp_path / "cli"
    cases = [
        (dict(config_root=cfg, cli_root=cli), env, cfg),
        (dict(cli_root=cli), env, cli),
        ({}, env, tmp_path / "env"),
        ({}, {SKILLS_ROOT_ENV: ""}, project),
        ({}, {}, project),
    ]
    for kwargs, environ, expected in cases:
        got = resolve_skills_root(**kwargs, environ=environ, cwd=nested)
        assert got == expected.resolve(), (kwargs, environ)


def test_install_skills_with_no_root_follows_the_env_var(
    tmp_path: Path, write_skill: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "env-root"
    write_skill(SkillStore(root).live, "env-skill")
    monkeypatch.setenv(SKILLS_ROOT_ENV, str(root))
    builder = RecordingBuilder()
    install_skills(SkillsConfig(), builder)
    (ext,) = builder.contributors
    assert isinstance(ext, SkillsExtension)
    assert ext._store.root == root.resolve()
    assert [e.name for e in ext._store.catalog_entries()] == ["env-skill"]


# --- stats sidecar -------------------------------------------------------------


def test_counters_live_only_in_the_sidecar(
    fixture_store: SkillStore,
) -> None:
    before = tree_hash(fixture_store.live)
    fixture_store.record_use("jwt-decode", True)
    fixture_store.record_use("jwt-decode", False)
    assert tree_hash(fixture_store.live) == before
    assert fixture_store.stats_path.parent == fixture_store.meta
    assert fixture_store.read_stats()["jwt-decode"].usage_count == 2
    assert not list(fixture_store.live.rglob("stats.json"))


def test_stats_file_is_sorted_json_and_round_trips(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    when = datetime(2026, 11, 20, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    store.record_use("zulu", True, now=when)
    store.record_use("alpha", False, now=when)
    raw = json.loads(store.stats_path.read_text(encoding="utf-8"))
    assert list(raw) == ["alpha", "zulu"]
    stats = store.read_stats()
    assert stats["zulu"] == SkillStats(usage_count=1, success_count=1, last_used=when)
    assert stats["zulu"].last_used is not None
    assert stats["zulu"].last_used.utcoffset() == timedelta(hours=5)
    assert stats["alpha"].success_count == 0


def test_record_use_defaults_last_used_to_now_utc(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    before = datetime.now(UTC)
    stats = store.record_use("x", True)
    assert stats.last_used is not None and stats.last_used >= before
    assert stats.last_used.tzinfo is not None


def test_many_writes_leave_no_temp_files(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    for i in range(25):
        store.record_use(f"skill-{i % 4}", i % 2 == 0)
    assert sorted(p.name for p in store.meta.iterdir()) == ["stats.json"]
    assert sum(s.usage_count for s in store.read_stats().values()) == 25


def test_extra_keys_in_stats_are_ignored(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.meta.mkdir(parents=True)
    store.stats_path.write_bytes(
        b'{"a": {"usage_count": 3, "success_count": 1, "rating": 5}}'
    )
    assert store.read_stats() == {"a": SkillStats(usage_count=3, success_count=1)}


@pytest.mark.parametrize(
    "content",
    [
        b'[{"usage_count": 1}]',
        b'{"good": {"usage_count": 2}, "bad": {"usage_count": "many"}}',
        b'{"a": null}',
        b'{"a": {"usage_count": 1}',
        b"",
        b"\xff\xfe\x00garbage",
    ],
    ids=["list", "one-bad-entry", "null-entry", "truncated", "empty", "binary"],
)
def test_corrupt_stats_read_as_empty_as_a_whole(tmp_path: Path, content: bytes) -> None:
    store = SkillStore(tmp_path)
    store.meta.mkdir(parents=True)
    store.stats_path.write_bytes(content)
    assert store.read_stats() == {}


def test_next_record_after_corruption_resets_every_counter(tmp_path: Path) -> None:
    """Documented T049 tradeoff: counters are advisory."""
    store = SkillStore(tmp_path)
    store.meta.mkdir(parents=True)
    store.stats_path.write_bytes(
        b'{"kept": {"usage_count": 9}, "bad": {"usage_count": -1.5}}'
    )
    store.record_use("new", True)
    assert set(store.read_stats()) == {"new"}


def test_transient_read_error_resets_every_counter_on_next_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins current behaviour (T049 'unreadable reads as empty').

    On Windows a reader gets a transient PermissionError while another
    writer's ``os.replace`` is in flight (T054 measured 234 of 5,442 reads),
    so concurrent ``record_use`` calls can wipe every other skill's counters.
    """
    store = SkillStore(tmp_path)
    store.record_use("kept", True)
    real_read_text = Path.read_text
    calls = {"n": 0}

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == store.stats_path and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(13, "Access is denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    store.record_use("other", True)
    monkeypatch.undo()
    assert set(store.read_stats()) == {"other"}


def test_stats_path_that_is_a_directory(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.stats_path.mkdir(parents=True)
    assert store.read_stats() == {}
    with pytest.raises(OSError):
        store.record_use("x", True)
    assert [p.name for p in store.meta.iterdir()] == ["stats.json"]


def hammer(store: SkillStore) -> tuple[list[BaseException], list[str]]:
    """Four threads x 40 ``record_use`` while one thread reads the raw file.

    Returns (exceptions raised by writers, torn file snapshots).
    """
    store.record_use("seed", True)
    errors: list[BaseException] = []
    torn: list[str] = []
    stop = threading.Event()

    def writer(name: str) -> None:
        for _ in range(40):
            try:
                store.record_use(name, True)
            except OSError as exc:
                errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = store.stats_path.read_bytes()
            except OSError:
                continue
            try:
                json.loads(raw)
            except ValueError:
                torn.append(raw[:80].decode("utf-8", "replace"))

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(4)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()
    return errors, torn


def test_concurrent_writers_never_tear_the_file(tmp_path: Path) -> None:
    """Lost increments are an accepted tradeoff; a torn file is not."""
    store = SkillStore(tmp_path)
    _, torn = hammer(store)
    assert torn == []
    assert sorted(p.name for p in store.meta.iterdir()) == ["stats.json"]
    final = store.read_stats()
    assert final["seed"].usage_count == 1
    for name, stats in final.items():
        assert 1 <= stats.usage_count <= 40, name


@pytest.mark.xfail(
    sys.platform == "win32",
    strict=False,
    reason=(
        "T054 source finding: on Windows os.replace onto stats.json raises "
        "PermissionError (WinError 5) while another writer or reader has it "
        "open; write_stats has no retry (promotion.replace_path has one) and "
        "the skills:stats hook error is swallowed by HookManager, so the use "
        "is lost"
    ),
)
def test_concurrent_writers_do_not_fail(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    errors, _ = hammer(store)
    assert errors == [], [repr(e) for e in errors[:3]]
