import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from store_contract import make_entry

from shared_context import cli
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
            cfg=Config(store_path=tmp_path / "store"),
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
    for argv in (["held"], ["usage"], ["reindex"]):
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
