import json
from pathlib import Path

import pytest
from store_contract import make_entry

from shared_context import promote
from shared_context.models import Status
from shared_context.store.memory import InMemoryStore


class FakeGh:
    """Patches promote.run_command; records (argv, cwd, env, timeout) and returns canned output."""

    def __init__(self, monkeypatch, rc=0, out=None, which="gh"):
        self.calls = []
        self.rc = rc
        self.out = out if out is not None else json.dumps({"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"})
        monkeypatch.setattr(promote.shutil, "which", lambda name: which)
        monkeypatch.setattr(promote, "run_command", self._run)

    def _run(self, argv, cwd, env, timeout):
        self.calls.append((argv, cwd, env, timeout))
        out = self.out(argv) if callable(self.out) else self.out
        return self.rc, out


def test_pr_merged_true_false_none(monkeypatch):
    gh = FakeGh(monkeypatch, out=json.dumps({"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}))
    assert promote.pr_merged("pr:acme/api#12") is True
    argv, cwd, env, timeout = gh.calls[0]
    assert argv[1:5] == ["pr", "view", "12", "--repo"] and argv[5] == "acme/api"
    assert argv[-2:] == ["--json", "state,mergedAt"]
    assert env["GH_PROMPT_DISABLED"] == "1" and 0 < timeout <= 15
    gh.out = json.dumps({"state": "OPEN", "mergedAt": None})
    assert promote.pr_merged("pr:acme/api#12") is False
    gh.rc, gh.out = 1, "not logged in"
    assert promote.pr_merged("pr:acme/api#12") is None


def test_pr_merged_closed_unmerged_is_false(monkeypatch):
    FakeGh(monkeypatch, out=json.dumps({"state": "CLOSED", "mergedAt": None}))
    assert promote.pr_merged("pr:acme/api#3") is False


def test_pr_merged_merged_at_alone_counts(monkeypatch):
    FakeGh(monkeypatch, out=json.dumps({"state": "X", "mergedAt": "2026-01-01T00:00:00Z"}))
    assert promote.pr_merged("pr:acme/api#3") is True


def test_pr_merged_none_when_gh_missing(monkeypatch):
    gh = FakeGh(monkeypatch, which=None)
    assert promote.pr_merged("pr:acme/api#3") is None
    assert gh.calls == []


@pytest.mark.parametrize("out", ["not json", "[]", '"x"', "", '{"state": 5}'])
def test_pr_merged_bad_output_is_none(monkeypatch, out):
    FakeGh(monkeypatch, out=out)
    assert promote.pr_merged("pr:acme/api#3") is None


def test_pr_merged_tolerates_stderr_noise_before_json(monkeypatch):
    FakeGh(monkeypatch, out='warning: something\n{"state": "MERGED", "mergedAt": null}')
    assert promote.pr_merged("pr:acme/api#3") is True


@pytest.mark.parametrize("link", [
    "pr:--repo evil#1", "pr:acme/api#--flag", "pr:acme/api#", "pr:acme/api#1x", "pr:#1",
    "pr:-evil/api#1", "pr:acme/-api#1", "pr:acme/api/extra#1", "pr:acme api#1", "pr:acme/api#1\n",
    "http://x/pull/1", "", "pr:", "pr:a;b#1", "pr:../x#1", "pr:acme/api#" + "1" * 12,
])
def test_pr_merged_rejects_bad_links_without_running(monkeypatch, link):
    gh = FakeGh(monkeypatch)
    assert promote.pr_merged(link) is None
    assert gh.calls == []


def test_pr_merged_never_raises(monkeypatch):
    monkeypatch.setattr(promote.shutil, "which", lambda n: "gh")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(promote, "run_command", boom)
    assert promote.pr_merged("pr:acme/api#1") is None
    assert promote.pr_merged(None) is None  # type: ignore[arg-type]


def test_bare_repo_without_roots_is_none(monkeypatch):
    gh = FakeGh(monkeypatch)
    assert promote.pr_merged("pr:api#7") is None
    assert promote.pr_merged("pr:api#7", repo_roots={"other": Path(".")}) is None
    assert gh.calls == []


def test_bare_repo_resolved_via_repo_roots_uses_cwd(monkeypatch, tmp_path):
    gh = FakeGh(monkeypatch)
    assert promote.pr_merged("pr:api#7", repo_roots={"api": tmp_path}) is True
    argv, cwd, _, _ = gh.calls[0]
    assert argv[1:4] == ["pr", "view", "7"] and "--repo" not in argv
    assert Path(cwd) == tmp_path


def test_bare_repo_root_missing_dir_is_none(monkeypatch, tmp_path):
    gh = FakeGh(monkeypatch)
    assert promote.pr_merged("pr:api#7", repo_roots={"api": tmp_path / "nope"}) is None
    assert gh.calls == []


def _pr_entry(eid, links, **kw):
    return make_entry(eid, evidence=links, **kw)


def test_promote_confirms_merged_pr_entries(monkeypatch):
    store = InMemoryStore()
    store.write_entry(_pr_entry("E1", ["pr:acme/api#1"]))
    store.write_entry(_pr_entry("E2", ["pr:acme/api#2", "pr:acme/api#1"]))
    store.write_entry(_pr_entry("E3", ["pr:acme/api#9"]))
    store.write_entry(_pr_entry("E4", ["repo:api@abc:x.py"]))
    FakeGh(monkeypatch, out=lambda argv: json.dumps(
        {"state": "MERGED" if argv[3] in ("1", "2") else "OPEN", "mergedAt": None}))
    assert promote.promote_entries(store) == 2
    assert store.get_entry("E1").status == Status.CONFIRMED
    assert store.get_entry("E2").status == Status.CONFIRMED
    assert store.get_entry("E3").status == Status.UNVERIFIED
    assert store.get_entry("E4").status == Status.UNVERIFIED


def test_promote_skips_when_gh_unavailable(monkeypatch):
    store = InMemoryStore()
    store.write_entry(_pr_entry("E1", ["pr:acme/api#1"]))
    FakeGh(monkeypatch, which=None)
    assert promote.promote_entries(store) == 0
    assert store.get_entry("E1").status == Status.UNVERIFIED


def test_promote_never_touches_held_or_stale_or_confirmed(monkeypatch):
    store = InMemoryStore()
    store.write_entry(_pr_entry("H", ["pr:acme/api#1"], status=Status.HELD, hold_reason="r"))
    store.write_entry(_pr_entry("S", ["pr:acme/api#1"], status=Status.STALE, stale_reason="r"))
    store.write_entry(_pr_entry("C", ["pr:acme/api#1"], status=Status.CONFIRMED))
    gh = FakeGh(monkeypatch)
    assert promote.promote_entries(store) == 0
    assert gh.calls == []
    assert store.get_entry("H").status == Status.HELD
    assert store.get_entry("S").status == Status.STALE


def test_promote_caches_per_link_and_survives_errors(monkeypatch):
    store = InMemoryStore()
    store.write_entry(_pr_entry("E1", ["pr:acme/api#1"]))
    store.write_entry(_pr_entry("E2", ["pr:acme/api#1"]))
    gh = FakeGh(monkeypatch)
    assert promote.promote_entries(store) == 2
    assert len(gh.calls) == 1

    class Broken(InMemoryStore):
        def iter_entries(self, status=None):
            raise OSError("x")

    assert promote.promote_entries(Broken()) == 0


def test_promote_set_status_failure_not_counted(monkeypatch):
    class Flaky(InMemoryStore):
        def set_status(self, *a, **k):
            raise OSError("disk")

    store = Flaky()
    store.write_entry(_pr_entry("E1", ["pr:acme/api#1"]))
    FakeGh(monkeypatch)
    assert promote.promote_entries(store) == 0
