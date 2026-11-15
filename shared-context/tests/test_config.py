from pathlib import Path

from shared_context.config import Config, load_config


def test_env_overrides_store_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SHARED_CONTEXT_STORE", str(tmp_path / "store"))
    assert load_config().store_path == tmp_path / "store"


def test_defaults_match_spec():
    c = Config(store_path=Path("x"))
    assert (c.capture_min_interval_s, c.pull_min_interval_s, c.top_k, c.token_cap) == (90, 45, 3, 800)
    assert (c.stale_days, c.body_soft_cap, c.max_calls_per_session, c.max_transcript_chars) == (
        90, 2048, 20, 60000)


def test_state_dir_default_and_env_override(monkeypatch, tmp_path):
    from shared_context.config import index_path, resolve_state_dir

    monkeypatch.delenv("SHARED_CONTEXT_STATE", raising=False)
    assert resolve_state_dir() == Path.home() / ".shared-context" / "state"
    monkeypatch.setenv("SHARED_CONTEXT_STATE", str(tmp_path / "st"))
    assert resolve_state_dir() == tmp_path / "st"
    assert index_path(tmp_path / "st") == tmp_path / "st" / "index" / "index.db"
