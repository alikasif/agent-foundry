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
