import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from store_contract import make_entry

from shared_context import cli, promote
from shared_context.config import Config
from shared_context.models import Status
from shared_context.store.git import GitStore
from shared_context.store.memory import InMemoryStore

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ESC = "\x1b[31mRED\x1b[0m\x07\x1b]0;pwned\x07"


class Env:
    def __init__(self, store, tmp_path, monkeypatch, capsys):
        self.store = store
        self.published = []
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.usage = self.state / "usage.jsonl"
        self.capsys = capsys
        self.ctx = cli.Context(
            store=store,
            cfg=Config(store_path=tmp_path / "store", promote_owners=["acme"]),
            state_dir=self.state,
            usage_path=self.usage,
            publish=lambda msg: self.published.append(msg) or True,
            author=lambda: "me",
            now=lambda: NOW,
        )
        (tmp_path / "store").mkdir(exist_ok=True)
        monkeypatch.setattr(cli, "_open_context", lambda: self.ctx)

    def run(self, *argv):
        rc = cli.main(list(argv))
        cap = self.capsys.readouterr()
        return rc, cap.out, cap.err


@pytest.fixture(params=["memory", "git"])
def env(request, tmp_path, monkeypatch, capsys):
    if request.param == "memory":
        store = InMemoryStore()
    else:
        store = GitStore(tmp_path / "store", tmp_path / "state" / "index.db", now=lambda: NOW)
    return Env(store, tmp_path, monkeypatch, capsys)


def held(eid="H1", **kw):
    kw.setdefault("hold_reason", "scrubber: credential")
    kw.setdefault("created", "2026-11-19T10:00:00Z")
    return make_entry(eid, status=Status.HELD, **kw)


def fresh(eid="E1", **kw):
    kw.setdefault("created", "2026-11-19T10:00:00Z")
    return make_entry(eid, **kw)


# ----------------------------------------------------------------- held list
def test_held_lists_id_title_reason_only(env):
    env.store.write_entry(held("H1", title="Needs review", body="SECRET BODY TEXT"))
    env.store.write_entry(fresh("E1", title="not held"))
    rc, out, _ = env.run("held")
    assert rc == 0
    assert "H1" in out and "Needs review" in out and "scrubber: credential" in out
    assert "SECRET BODY TEXT" not in out and "not held" not in out


def test_held_empty(env):
    rc, out, _ = env.run("held")
    assert rc == 0 and "No held" in out


def test_held_strips_terminal_escapes_and_bounds_length(env):
    env.store.write_entry(held("H1", title=f"{ESC} title " + "x" * 500, hold_reason="why\x1b[2J\nnew line"))
    rc, out, _ = env.run("held")
    assert rc == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "title" in out
    assert max(len(line) for line in out.splitlines()) < 400
    assert "why" in out and "new line" in out
    assert len([ln for ln in out.splitlines() if "H1" in ln]) == 1


def test_held_has_no_show_subcommand(env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["held", "show", "H1"])
    assert exc.value.code == 2


# -------------------------------------------------------------------- reindex
def test_reindex_refreshes_and_prints_count(env, monkeypatch):
    env.store.write_entry(fresh("E1"))
    env.store.write_entry(fresh("E2"))
    env.store.write_entry(held("H1"))
    calls = []
    real = env.store.refresh
    monkeypatch.setattr(env.store, "refresh", lambda: calls.append(1) or real(), raising=False)
    rc, out, _ = env.run("reindex")
    assert rc == 0 and calls == [1]
    assert "2 entries" in out


# ---------------------------------------------------------------------- usage
def _log(env, *lines):
    env.usage.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rec(entry_id, kind="use", days_ago=1, session="s1"):
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return json.dumps({"ts": ts, "entry_id": entry_id, "session_id": session, "kind": kind})


def test_usage_lists_local_log(env):
    env.store.write_entry(fresh("E1", title="Retry policy"))
    env.store.write_entry(fresh("E2", title="Other note"))
    _log(env, _rec("E1"), _rec("E1"), _rec("E1", "feedback_helpful"), _rec("E2"))
    rc, out, _ = env.run("usage")
    assert rc == 0
    e1 = next(ln for ln in out.splitlines() if "E1" in ln)
    assert "Retry policy" in e1 and "use=2" in e1 and "feedback_helpful=1" in e1
    e2 = next(ln for ln in out.splitlines() if "E2" in ln)
    assert "use=1" in e2
    assert out.index("E1") < out.index("E2")


def test_usage_days_filter(env):
    env.store.write_entry(fresh("E1", title="Recent"))
    env.store.write_entry(fresh("E2", title="Old"))
    _log(env, _rec("E1", days_ago=1), _rec("E2", days_ago=10))
    rc, out, _ = env.run("usage")
    assert "E1" in out and "E2" not in out
    rc, out, _ = env.run("usage", "--days", "30")
    assert "E1" in out and "E2" in out
    for bad in ("0", "-3", "abc"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["usage", "--days", bad])
        assert exc.value.code == 2
    env.capsys.readouterr()


def test_usage_tolerates_malformed_lines(env):
    env.store.write_entry(fresh("E1", title="Fine"))
    _log(env, "not json", "[]", "42", json.dumps({"entry_id": "E1"}), json.dumps({"ts": "garbage", "entry_id": "E1"}),
         json.dumps({"ts": NOW.isoformat(), "entry_id": 5, "kind": "use"}), "", _rec("E1"))
    rc, out, err = env.run("usage")
    assert rc == 0 and "E1" in out and "use=1" in out
    assert "Traceback" not in out + err


def test_usage_missing_log(env):
    rc, out, _ = env.run("usage")
    assert rc == 0 and "No usage" in out


def test_usage_strips_escapes_from_titles_and_unknown_entries(env):
    env.store.write_entry(fresh("E1", title=f"{ESC} hi"))
    _log(env, _rec("E1"), _rec("GONE"))
    rc, out, _ = env.run("usage")
    assert rc == 0 and "\x1b" not in out and "\x07" not in out
    assert "GONE" in out


# ----------------------------------------------------------------- store state
def test_store_not_configured_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHARED_CONTEXT_STORE", str(tmp_path / "no-clone"))
    monkeypatch.setenv("SHARED_CONTEXT_STATE", str(tmp_path / "state"))
    for argv in (["held"], ["usage"], ["reindex"], ["promote"], ["confirm", "x"], ["stale", "x"],
                 ["held", "approve", "x"], ["held", "reject", "x"]):
        rc = cli.main(argv)
        cap = capsys.readouterr()
        assert rc == 1, argv
        assert "not configured" in cap.err
        assert "Traceback" not in cap.err
    assert not (tmp_path / "no-clone").exists()


def test_unexpected_error_is_short_message_not_traceback(env, monkeypatch):
    def boom():
        raise OSError("disk exploded with /secret/path")

    monkeypatch.setattr(env.store, "list_held", boom)
    rc, out, err = env.run("held")
    assert rc == 1
    assert "OSError" in err and "Traceback" not in err and "secret/path" not in err


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


# ------------------------------------------------------------- held approve
def test_held_approve_moves_to_entries_as_unverified(env):
    env.store.write_entry(held("H1", title="Clean title", body="plain body"))
    rc, out, _ = env.run("held", "approve", "H1")
    e = env.store.get_entry("H1")
    assert rc == 0 and e.status == Status.UNVERIFIED and e.hold_reason is None
    assert e.body == "plain body"
    assert "H1" in out and "unverified" in out
    assert env.store.list_held() == []
    assert [h.entry.id for h in env.store.search("clean title")] == ["H1"]
    assert len(env.published) == 1


def test_held_approve_rescrubs_hand_edited_secret_and_refuses_without_force(env):
    env.store.write_entry(held(
        "H1", title="token note", body="use password=hunter2secret here",
        applies_to=["db password=sup3rsecretpw"], evidence=["see api_key=abcd1234efgh5678"]))
    rc, out, err = env.run("held", "approve", "H1")
    assert rc == 1 and "--force" in err
    e = env.store.get_entry("H1")
    assert e.status == Status.HELD  # still not released
    assert "hunter2secret" not in e.body  # but the redacted text is what is on disk
    assert "sup3rsecretpw" not in "".join(e.applies_to) and "abcd1234efgh5678" not in "".join(e.evidence)
    assert "hunter2secret" not in out + err
    assert len(env.published) == 1  # the redaction itself is pushed; the entry is not released
    assert env.store.search("password") == []


def test_held_approve_force_releases_redacted_version(env):
    env.store.write_entry(held("H1", body="use password=hunter2secret here"))
    rc, out, err = env.run("held", "approve", "H1", "--force")
    e = env.store.get_entry("H1")
    assert rc == 0 and e.status == Status.UNVERIFIED
    assert "hunter2secret" not in e.body and "[REDACTED]" in e.body
    assert "redacted" in out.lower() and "body" in out and "hunter2secret" not in out


def test_held_approve_uses_store_denylist(env):
    (env.ctx.cfg.store_path / "config.yaml").write_text("denylist:\n  - acme-customer\n", encoding="utf-8")
    env.store.write_entry(held("H1", body="we talked to acme-customer about it"))
    rc, _, err = env.run("held", "approve", "H1")
    assert rc == 1 and "customer_data" in err
    assert "acme-customer" not in env.store.get_entry("H1").body
    rc, _, _ = env.run("held", "approve", "H1", "--force")
    assert rc == 0 and env.store.get_entry("H1").status == Status.UNVERIFIED


def test_held_approve_bad_rules_file_refuses(env):
    (env.ctx.cfg.store_path / "config.yaml").write_text("denylist: 5\n", encoding="utf-8")
    env.store.write_entry(held("H1"))
    rc, _, err = env.run("held", "approve", "H1")
    assert rc == 1 and "ValueError" in err and "Traceback" not in err
    assert env.store.get_entry("H1").status == Status.HELD


@pytest.mark.parametrize("sub", ["approve", "reject"])
def test_held_approve_reject_unknown_or_not_held(env, sub):
    env.store.write_entry(fresh("E1"))
    for bad in ("nope", "E1"):
        rc, out, err = env.run("held", sub, bad)
        assert rc == 1 and "not found" in err.lower() and "Traceback" not in err
    assert env.store.get_entry("E1").status == Status.UNVERIFIED
    assert env.published == []


def test_held_reject_removes(env):
    env.store.write_entry(held("H1", title="payments retry secret", body="payments retry"))
    rc, out, _ = env.run("held", "reject", "H1")
    assert rc == 0 and "H1" in out
    assert env.store.get_entry("H1") is None
    assert env.store.list_held() == []
    assert env.store.search("payments retry") == []
    assert len(env.published) == 1


def test_held_reject_unretrievable_via_mcp_read_and_search(env):
    from shared_context.mcp_server import Tools

    env.store.write_entry(held("H1", title="payments retry", body="payments retry"))
    env.run("held", "reject", "H1")
    tools = Tools(env.store, env.ctx.cfg, session_id="s", author=lambda: "me")
    assert tools.read_learning("H1") == "Learning not found."
    assert "No learnings found" in tools.search_learnings("payments retry")


def test_cli_publish_uses_shared_locked_helper(tmp_path, monkeypatch):
    """I2: the CLI publishes through hooks.publish_store (store.lock, commit, rebase, push)."""
    from shared_context import hooks

    cfg = Config(store_path=tmp_path / "store")
    calls = []
    monkeypatch.setenv("SHARED_CONTEXT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(hooks, "_load_cfg", lambda c: cfg)
    monkeypatch.setattr(hooks, "_default_store",
                        lambda c, s: GitStore(tmp_path / "store", tmp_path / "idx" / "index.db"))
    monkeypatch.setattr(hooks, "publish_store", lambda c, s, m, **kw: calls.append((c, s, m)) or True)
    ctx = cli._open_context()
    assert ctx.publish("shared-context: confirm entry") is True
    assert calls == [(cfg, tmp_path / "state", "shared-context: confirm entry")]


def test_reject_git_store_deletes_file(tmp_path, monkeypatch, capsys):
    store = GitStore(tmp_path / "store", tmp_path / "state" / "index.db", now=lambda: NOW)
    e = Env(store, tmp_path, monkeypatch, capsys)
    store.write_entry(held("H1"))
    assert (tmp_path / "store" / "held" / "H1.md").is_file()
    assert e.run("held", "reject", "H1")[0] == 0
    assert not (tmp_path / "store" / "held" / "H1.md").exists()
    assert not (tmp_path / "store" / "entries" / "H1.md").exists()


# ------------------------------------------------------------------ confirm
def test_confirm_marks_confirmed(env):
    env.store.write_entry(fresh("E1", author="alice"))
    rc, out, _ = env.run("confirm", "E1")
    assert rc == 0 and "E1" in out
    assert env.store.get_entry("E1").status == Status.CONFIRMED
    assert len(env.published) == 1


def test_confirm_refuses_self_confirmation(env):
    env.store.write_entry(fresh("E1", author="me"))
    rc, _, err = env.run("confirm", "E1")
    assert rc == 1 and "author" in err and "--allow-self" in err
    assert env.store.get_entry("E1").status == Status.UNVERIFIED
    assert env.published == []


def test_confirm_allow_self(env):
    env.store.write_entry(fresh("E1", author="me"))
    rc, _, _ = env.run("confirm", "E1", "--allow-self")
    assert rc == 0 and env.store.get_entry("E1").status == Status.CONFIRMED


@pytest.mark.parametrize("status,extra", [
    (Status.HELD, {"hold_reason": "r"}), (Status.STALE, {"stale_reason": "r"}), (Status.CONFIRMED, {})])
def test_confirm_refuses_ineligible_statuses(env, status, extra):
    env.store.write_entry(make_entry("E1", status=status, author="alice", created="2026-11-19T10:00:00Z", **extra))
    rc, _, err = env.run("confirm", "E1", "--allow-self")
    assert rc == 1 and err.strip()
    assert env.store.get_entry("E1").status == status
    assert env.published == []


def test_confirm_unknown_id(env):
    rc, _, err = env.run("confirm", "nope")
    assert rc == 1 and "not found" in err.lower()


def test_confirm_help_documents_allow_self(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["confirm", "--help"])
    assert exc.value.code == 0
    assert "--allow-self" in capsys.readouterr().out


def test_mcp_and_cli_share_confirm_rule():
    from shared_context import mcp_server

    assert mcp_server.confirm_refusal is promote.confirm_refusal


# -------------------------------------------------------------------- stale
def test_stale_sets_reason(env):
    env.store.write_entry(fresh("E1"))
    rc, out, _ = env.run("stale", "E1", "--reason", "API was removed\x1b[2J")
    e = env.store.get_entry("E1")
    assert rc == 0 and e.status == Status.STALE
    assert e.stale_reason == "API was removed[2J"
    assert "\x1b" not in out
    assert len(env.published) == 1


def test_stale_default_reason(env):
    env.store.write_entry(fresh("E1"))
    assert env.run("stale", "E1")[0] == 0
    assert env.store.get_entry("E1").stale_reason


def test_stale_refuses_held_and_unknown(env):
    env.store.write_entry(held("H1"))
    rc, _, err = env.run("stale", "H1")
    assert rc == 1 and "held" in err
    assert env.store.get_entry("H1").status == Status.HELD
    rc, _, err = env.run("stale", "nope")
    assert rc == 1 and "not found" in err.lower()


# ------------------------------------------------------------------ promote
def test_promote_command_prints_count(env, monkeypatch):
    monkeypatch.setattr(promote, "gh_available", lambda: True)
    monkeypatch.setattr(promote, "promote_entries", lambda store, **kw: 2)
    rc, out, _ = env.run("promote")
    assert rc == 0 and "2" in out
    assert len(env.published) == 1


def test_promote_command_zero_does_not_publish(env, monkeypatch):
    monkeypatch.setattr(promote, "gh_available", lambda: True)
    monkeypatch.setattr(promote, "promote_entries", lambda store, **kw: 0)
    rc, out, _ = env.run("promote")
    assert rc == 0 and "0" in out and env.published == []


def test_promote_command_gh_unavailable(env, monkeypatch):
    monkeypatch.setattr(promote, "gh_available", lambda: False)
    called = []
    monkeypatch.setattr(promote, "promote_entries", lambda store, **kw: called.append(1) or 0)
    rc, out, err = env.run("promote")
    assert rc == 1 and "gh" in err and called == []


def test_promote_end_to_end_with_patched_gh(env, monkeypatch):
    env.store.write_entry(fresh("E1", evidence=["pr:acme/api#4"]))
    monkeypatch.setattr(promote.shutil, "which", lambda n: "gh")
    monkeypatch.setattr(promote, "run_command",
                        lambda argv, cwd, e, t: (0, json.dumps({"state": "MERGED", "mergedAt": None})))
    rc, out, _ = env.run("promote")
    assert rc == 0 and "1" in out
    assert env.store.get_entry("E1").status == Status.CONFIRMED


def test_promote_without_owner_allow_list_does_nothing(env, monkeypatch):
    import dataclasses

    env.ctx.cfg = dataclasses.replace(env.ctx.cfg, promote_owners=[])
    env.store.write_entry(fresh("E1", evidence=["pr:acme/api#4"]))
    monkeypatch.setattr(promote, "gh_available", lambda: True)
    monkeypatch.setattr(promote, "run_command", lambda *a: pytest.fail("gh must not run"))
    rc, out, _ = env.run("promote")
    assert rc == 0 and "promote_owners" in out
    assert env.store.get_entry("E1").status == Status.UNVERIFIED and env.published == []


def test_promote_passes_owners_and_repo_roots(env, monkeypatch, tmp_path):
    import dataclasses

    env.ctx.cfg = dataclasses.replace(env.ctx.cfg, repo_roots={"api": tmp_path})
    seen = {}
    monkeypatch.setattr(promote, "gh_available", lambda: True)
    monkeypatch.setattr(promote, "promote_entries", lambda store, **kw: seen.update(kw) or 0)
    env.run("promote")
    assert seen == {"owners": ["acme"], "repo_roots": {"api": tmp_path}}


def test_publish_failure_is_a_note_not_an_error(env):
    env.ctx.publish = lambda msg: False
    env.store.write_entry(fresh("E1", author="alice"))
    rc, out, _ = env.run("confirm", "E1")
    assert rc == 0 and "saved locally" in out


# ---- real git: CLI publishes through hooks.publish_store (final review minor + ruling R9) ----

@pytest.fixture
def git_env(tmp_path, monkeypatch, capsys):
    from sim_support import git, isolate, make_remote

    from shared_context import hooks

    isolate(monkeypatch, tmp_path)
    remote = make_remote(tmp_path)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    git(clone, "config", "user.name", "Me")
    git(clone, "config", "user.email", "me@example.com")
    store = GitStore(clone, tmp_path / "state" / "index" / "index.db", now=lambda: NOW)
    env = Env(store, tmp_path, monkeypatch, capsys)
    cfg = Config(store_path=clone)
    env.ctx.cfg = cfg
    env.ctx.publish = lambda msg: hooks.publish_store(cfg, env.state, msg, wait_s=5)
    env.remote, env.clone, env.git = remote, clone, git
    return env


def _remote_tree(env) -> list[str]:
    return env.git(env.remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def test_real_git_held_approve_pushes_released_entry(git_env):
    git_env.store.write_entry(held("H1", title="Clean title", body="plain body"))
    rc, out, _ = git_env.run("held", "approve", "H1")
    assert rc == 0 and "saved locally" not in out
    assert "entries/H1.md" in _remote_tree(git_env)
    assert not any(p.startswith("held/") for p in _remote_tree(git_env))
    assert git_env.git(git_env.clone, "status", "--porcelain") == ""


def test_real_git_held_reject_never_touches_the_remote(git_env):
    before = git_env.git(git_env.remote, "rev-parse", "main")
    git_env.store.write_entry(held("H2", body="HELDBODYMARKER"))
    rc, out, _ = git_env.run("held", "reject", "H2")
    assert rc == 0 and not (git_env.clone / "held" / "H2.md").exists()
    assert git_env.git(git_env.remote, "rev-parse", "main") == before  # held was local only (R9)
    assert "HELDBODYMARKER" not in git_env.git(git_env.remote, "log", "--all", "-p")


def test_real_git_stale_publishes_change(git_env):
    git_env.store.write_entry(fresh("E1", author="alice"))
    assert git_env.ctx.publish("seed") is True
    rc, _, _ = git_env.run("stale", "E1", "--reason", "superseded")
    assert rc == 0
    text = git_env.git(git_env.remote, "show", "main:entries/E1.md")
    assert "status: stale" in text and "superseded" in text
