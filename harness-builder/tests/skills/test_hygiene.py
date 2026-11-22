"""T055: skill hygiene (FR-056): list/show/stats, dedup and prune.

``plan_prune`` and every read command write nothing (a dry run leaves the
skills root byte-for-byte unchanged); ``apply_prune`` only runs after an
explicit ``--apply`` plus a yes, and moves skills to the trash, never deletes.
CLI tests are sync (commands call ``anyio.run``) and always pass
``--skills-root``, so the repository's own ``.agents`` is never touched.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.errors import ConfigError
from harness_builder.skills import hygiene
from harness_builder.skills.hygiene import (
    apply_prune,
    find_duplicates,
    list_skills,
    pending_candidates,
    plan_prune,
    show_skill,
    skill_stats,
)
from harness_builder.skills.promotion import CANDIDATE_FILE, SOURCE_FILE
from harness_builder.skills.store import SkillStats, SkillStore

from .support import (  # pyrefly: ignore[missing-import]
    CLI_ENV,
    SOURCE,
    isolate_cli_env,
    live_names,
    make_candidate,
    promotion_service,
    tree_snapshot,
    write_live_skill,
)

pytestmark = pytest.mark.timeout(180)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=3)

CSV = "Merge two CSV files on a shared key column into one table."
CSV_TWIN = "Merge two CSV files on a shared key column into a single table."
PDF = "Extract the text of every page of a PDF document."
IMG = "Resize PNG and JPEG images to a maximum width."


def set_mtime(skill: Path, when: datetime) -> None:
    """Set ``SKILL.md``'s modification time (policy dates never use checkout)."""
    stamp = when.timestamp()
    os.utime(skill / "SKILL.md", (stamp, stamp))


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """An empty skills root under ``tmp_path``."""
    return SkillStore(tmp_path / "root")


@pytest.fixture
def seeded(store: SkillStore) -> SkillStore:
    """Four live skills (two near-duplicates) with recent mtimes and stats."""
    for name, description in (
        ("csv-merge", CSV),
        ("csv-join", CSV_TWIN),
        ("pdf-text", PDF),
        ("image-resize", IMG),
    ):
        set_mtime(write_live_skill(store, name, description), RECENT)
    store.write_stats(
        {
            "csv-merge": SkillStats(usage_count=4, success_count=4, last_used=RECENT),
            "pdf-text": SkillStats(usage_count=1, success_count=0, last_used=RECENT),
        }
    )
    return store


# --- list / show / stats ------------------------------------------------------


def test_list_skills_sorted_with_stats(seeded: SkillStore) -> None:
    infos = list_skills(seeded)
    assert [i.name for i in infos] == [
        "csv-join",
        "csv-merge",
        "image-resize",
        "pdf-text",
    ]
    by_name = {i.name: i for i in infos}
    assert by_name["csv-merge"].success_rate == 1.0
    assert by_name["pdf-text"].success_rate == 0.0
    assert by_name["image-resize"].success_rate is None
    assert by_name["csv-merge"].path == seeded.live / "csv-merge"


def test_show_skill_and_unknown_name(seeded: SkillStore) -> None:
    (seeded.live / "pdf-text" / "references").mkdir()
    (seeded.live / "pdf-text" / "references" / "notes.md").write_bytes(b"n\n")
    detail = show_skill(seeded, "pdf-text")
    assert detail.info.description == PDF
    assert "Body." in detail.body
    assert any("notes.md" in resource for resource in detail.resources)
    with pytest.raises(ConfigError, match="no live skill"):
        show_skill(seeded, "missing-skill")


def test_stats_reports_never_used_and_orphans(seeded: SkillStore) -> None:
    stats = seeded.read_stats()
    stats["gone-skill"] = SkillStats(usage_count=2, success_count=1)
    seeded.write_stats(stats)
    report = skill_stats(seeded)
    assert report.total_uses == 5
    assert report.never_used == ("csv-join", "image-resize")
    assert list(report.orphans) == ["gone-skill"]


# --- dedup ----------------------------------------------------------------------


def test_find_duplicates_reports_near_twins_only(seeded: SkillStore) -> None:
    pairs = find_duplicates(seeded)
    assert [(p.first, p.second) for p in pairs] == [("csv-join", "csv-merge")]
    assert pairs[0].similarity >= hygiene.DEDUP_THRESHOLD
    assert find_duplicates(seeded, threshold=1.0) == []


def test_dedup_changes_nothing(seeded: SkillStore) -> None:
    before = tree_snapshot(seeded.root)
    find_duplicates(seeded, threshold=0.0)
    assert tree_snapshot(seeded.root) == before


# --- prune planning ---------------------------------------------------------------


def test_plan_prune_writes_nothing_on_seeded_root(seeded: SkillStore) -> None:
    set_mtime(seeded.live / "image-resize", OLD)
    before = tree_snapshot(seeded.root)
    assert plan_prune(seeded, now=NOW)
    assert plan_prune(seeded, ("csv-join",), now=NOW)
    assert tree_snapshot(seeded.root) == before


def test_plan_prune_on_fresh_root_creates_nothing(store: SkillStore) -> None:
    store.root.mkdir(parents=True)
    assert plan_prune(store, now=NOW) == []
    assert list(store.root.iterdir()) == []  # no .agents, meta or trash dir


def test_never_used_skill_selected_by_skill_md_mtime(seeded: SkillStore) -> None:
    set_mtime(seeded.live / "image-resize", OLD)
    plan = plan_prune(seeded, now=NOW)
    assert [(c.name, c.reason) for c in plan] == [
        ("image-resize", "never used, unchanged for over 90 days")
    ]
    assert plan_prune(seeded, now=NOW, unused_days=365) == []


@pytest.mark.parametrize("aware", [True, False])
def test_skill_unused_since_last_use_selected(seeded: SkillStore, aware: bool) -> None:
    last = OLD if aware else OLD.replace(tzinfo=None)
    stats = seeded.read_stats()
    stats["csv-merge"] = SkillStats(usage_count=4, success_count=4, last_used=last)
    seeded.write_stats(stats)
    plan = plan_prune(seeded, now=NOW)
    assert [(c.name, c.reason) for c in plan] == [
        ("csv-merge", "not used for over 90 days")
    ]


@pytest.mark.parametrize(
    ("uses", "successes", "selected"),
    [
        (5, 2, True),  # 40% over 5 uses
        (10, 4, True),
        (5, 3, False),  # 60%
        (4, 0, False),  # below min_uses
        (6, 3, False),  # exactly 50% is not below
    ],
)
def test_low_success_rate_rule(
    seeded: SkillStore, uses: int, successes: int, selected: bool
) -> None:
    stats = seeded.read_stats()
    stats["pdf-text"] = SkillStats(
        usage_count=uses, success_count=successes, last_used=RECENT
    )
    seeded.write_stats(stats)
    plan = plan_prune(seeded, now=NOW)
    assert [c.name for c in plan] == (["pdf-text"] if selected else [])
    if selected:
        assert plan[0].reason.startswith("success rate")


def test_prune_by_name_dedupes_and_rejects_unknown(seeded: SkillStore) -> None:
    plan = plan_prune(seeded, ("pdf-text", "csv-join", "pdf-text"), now=NOW)
    assert [(c.name, c.reason) for c in plan] == [
        ("pdf-text", "selected by name"),
        ("csv-join", "selected by name"),
    ]
    with pytest.raises(ConfigError, match="no-such-skill"):
        plan_prune(seeded, ("pdf-text", "no-such-skill"), now=NOW)


@pytest.mark.filterwarnings("ignore:Skill name 'odd-skill' does not match")
def test_non_kebab_directory_is_never_pruned(store: SkillStore) -> None:
    odd = write_live_skill(store, "odd-skill", PDF)
    odd = odd.rename(store.live / "Odd_Skill")
    set_mtime(odd, OLD)
    assert plan_prune(store, now=NOW) == []
    with pytest.raises(ConfigError):
        plan_prune(store, ("odd-skill",), now=NOW)


# --- apply ----------------------------------------------------------------------


def test_apply_prune_moves_to_trash_and_keeps_counters(seeded: SkillStore) -> None:
    plan = plan_prune(seeded, ("pdf-text",), now=NOW)
    [pruned] = apply_prune(seeded, plan, now=NOW)
    assert pruned.name == "pdf-text"
    assert pruned.trashed.parent == seeded.trash
    assert pruned.trashed.name.startswith("pdf-text-")
    assert (pruned.trashed / "SKILL.md").read_text("utf-8").count(PDF) == 1
    assert not (seeded.live / "pdf-text").exists()
    assert "pdf-text" not in live_names(seeded)
    assert list(skill_stats(seeded).orphans) == ["pdf-text"]


def test_apply_prune_skips_a_vanished_skill(seeded: SkillStore) -> None:
    plan = plan_prune(seeded, ("pdf-text", "csv-join"), now=NOW)
    (seeded.live / "pdf-text").rename(seeded.root / "moved-away")
    pruned = apply_prune(seeded, plan, now=NOW)
    assert [p.name for p in pruned] == ["csv-join"]


# --- pending candidates -----------------------------------------------------------


@pytest.mark.anyio
async def test_pending_candidates_states(store: SkillStore) -> None:
    ready = await make_candidate(store)
    deferred = promotion_service(store).write_source(SOURCE)
    (store.pending / "zz-incomplete").mkdir()
    broken = store.pending / "zz-broken"
    broken.mkdir()
    (broken / CANDIDATE_FILE).write_text("{not json", encoding="utf-8")
    (store.pending / ".tmp-ignored").mkdir()  # not a candidate id
    (store.pending / "a-file").write_text("", encoding="utf-8")
    found = {p.cid: (p.state, p.name) for p in pending_candidates(store)}
    assert found == {
        ready: ("ready", "sum-numbers"),
        deferred: ("deferred", None),
        "zz-incomplete": ("incomplete", None),
        "zz-broken": ("incomplete", None),
    }
    assert (store.candidate_dir(deferred) / SOURCE_FILE).is_file()


def test_pending_candidates_without_pending_dir(store: SkillStore) -> None:
    assert pending_candidates(store) == []


# --- unreadable stats ---------------------------------------------------------


def corrupt_stats(store: SkillStore) -> None:
    store.stats_path.write_bytes(b'{"csv-merge": {"usage_count": ')


def deny_stats_reads(store: SkillStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(SkillStore, "read_stats", denied)


@pytest.mark.parametrize("how", ["corrupt", "denied"])
def test_unreadable_stats_are_reported_not_read_as_never_used(
    seeded: SkillStore, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    for skill in seeded.live.iterdir():
        set_mtime(skill, OLD)  # would all be prune candidates if stats read as {}
    if how == "corrupt":
        corrupt_stats(seeded)
    else:
        deny_stats_reads(seeded, monkeypatch)
    before = tree_snapshot(seeded.root)
    for call in (list_skills, skill_stats, plan_prune):
        with pytest.raises(ConfigError, match="stats unreadable"):
            call(seeded)
    with pytest.raises(ConfigError, match="stats unreadable"):
        show_skill(seeded, "csv-merge")
    with pytest.raises(ConfigError, match="stats unreadable"):
        plan_prune(seeded, ("csv-join",))
    assert tree_snapshot(seeded.root) == before


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: SkillStore) -> None:
    """Empty cwd and no ``HARNESS_*``/``OPENROUTER_*`` variables."""
    monkeypatch.chdir(isolate_cli_env(tmp_path, monkeypatch.delenv))


def harness(store: SkillStore, *args: str, stdin: str | None = None) -> Result:
    """Run ``harness skills <args> --skills-root <store.root>``."""
    argv = ["skills", *args, "--skills-root", str(store.root)]
    return CliRunner().invoke(cli_main.main, argv, env=CLI_ENV, input=stdin)


@pytest.mark.usefixtures("cli")
def test_cli_list_show_stats(seeded: SkillStore) -> None:
    listed = harness(seeded, "list")
    assert listed.exit_code == 0, listed.output
    assert "skills: 4" in listed.output and "csv-merge" in listed.output
    shown = harness(seeded, "show", "pdf-text")
    assert shown.exit_code == 0 and PDF in shown.output
    assert harness(seeded, "show", "missing-skill").exit_code == 2
    stats = harness(seeded, "stats")
    assert stats.exit_code == 0
    assert "total uses: 5" in stats.output
    assert "never used: csv-join, image-resize" in stats.output


@pytest.mark.usefixtures("cli")
@pytest.mark.parametrize("command", ["list", "stats", "prune"])
def test_cli_unreadable_stats_exit_2_and_prune_nothing(
    seeded: SkillStore, command: str
) -> None:
    for skill in seeded.live.iterdir():
        set_mtime(skill, OLD)
    corrupt_stats(seeded)
    before = tree_snapshot(seeded.root)
    result = harness(seeded, command)
    assert result.exit_code == 2
    assert "stats unreadable" in result.output
    assert tree_snapshot(seeded.root) == before


@pytest.mark.usefixtures("cli")
def test_cli_dedup_is_report_only(seeded: SkillStore) -> None:
    before = tree_snapshot(seeded.root)
    result = harness(seeded, "dedup")
    assert result.exit_code == 0
    assert (
        "csv-join ~ csv-merge" in result.output and "nothing changed" in result.output
    )
    assert harness(seeded, "dedup", "--threshold", "1").output.startswith(
        "no near-duplicate"
    )
    assert tree_snapshot(seeded.root) == before


@pytest.mark.usefixtures("cli")
def test_cli_prune_is_a_dry_run_by_default(seeded: SkillStore) -> None:
    set_mtime(seeded.live / "image-resize", OLD)
    before = tree_snapshot(seeded.root)
    result = harness(seeded, "prune")
    assert result.exit_code == 0
    assert "image-resize: never used" in result.output
    assert "dry run: 1 skill(s) would be pruned" in result.output
    named = harness(seeded, "prune", "csv-join", "--dry-run")
    assert "dry run" in named.output
    assert tree_snapshot(seeded.root) == before


@pytest.mark.usefixtures("cli")
@pytest.mark.parametrize("answer", ["n\n", "", "\n"], ids=["no", "eof", "default"])
def test_cli_prune_apply_without_yes_changes_nothing(
    seeded: SkillStore, answer: str
) -> None:
    before = tree_snapshot(seeded.root)
    result = harness(seeded, "prune", "pdf-text", "--apply", stdin=answer)
    assert result.exit_code == 0
    assert "nothing changed" in result.output
    assert tree_snapshot(seeded.root) == before
    assert "pdf-text" in live_names(seeded)


@pytest.mark.usefixtures("cli")
def test_cli_prune_apply_with_yes_moves_to_trash(seeded: SkillStore) -> None:
    result = harness(seeded, "prune", "pdf-text", "--apply", stdin="y\n")
    assert result.exit_code == 0, result.output
    assert "pruned pdf-text ->" in result.output
    assert live_names(seeded) == ["csv-join", "csv-merge", "image-resize"]
    [trashed] = list(seeded.trash.iterdir())
    assert trashed.name.startswith("pdf-text-")


@pytest.mark.usefixtures("cli")
def test_cli_prune_unknown_name_exits_2(seeded: SkillStore) -> None:
    before = tree_snapshot(seeded.root)
    result = harness(seeded, "prune", "no-such-skill", "--apply", stdin="y\n")
    assert result.exit_code == 2
    assert tree_snapshot(seeded.root) == before


@pytest.mark.usefixtures("cli")
def test_cli_prune_nothing_selected(seeded: SkillStore) -> None:
    result = harness(seeded, "prune", "--apply")
    assert result.exit_code == 0 and "nothing to prune" in result.output


def test_cli_prune_defaults_match_hygiene_policy() -> None:
    """The CLI repeats the policy defaults as literals; pin them together."""
    defaults = {param.name: param.default for param in cli_main.skills_prune.params}
    assert defaults["unused_days"] == hygiene.UNUSED_DAYS
    assert defaults["min_success_rate"] == hygiene.MIN_SUCCESS_RATE
    assert defaults["min_uses"] == hygiene.MIN_USES
    assert defaults["dry_run"] is True
