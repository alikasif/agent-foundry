import os
from pathlib import Path

import pytest

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


def _cfg_file(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_store_path_tilde_expanded_in_file_and_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("SHARED_CONTEXT_STORE", raising=False)
    cfg = load_config(_cfg_file(tmp_path, "store_path: ~/team-store\n"))
    assert cfg.store_path == Path(os.path.expanduser("~/team-store"))
    assert "~" not in str(cfg.store_path)
    monkeypatch.setenv("SHARED_CONTEXT_STORE", "~/env-store")
    assert "~" not in str(load_config(_cfg_file(tmp_path, "")).store_path)


@pytest.mark.parametrize("text", ['top_k: "3"\n', "top_k: true\n", "token_cap: 1.5\n", "store_path: 5\n",
                                  "pull_min_interval_s: null\n"])
def test_wrong_typed_values_rejected(monkeypatch, tmp_path, text):
    monkeypatch.delenv("SHARED_CONTEXT_STORE", raising=False)
    with pytest.raises(ValueError):
        load_config(_cfg_file(tmp_path, text))


def test_promote_owners_and_repo_roots_loaded(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("SHARED_CONTEXT_STORE", raising=False)
    cfg = load_config(_cfg_file(tmp_path, "promote_owners: [acme, Contoso]\n"
                                          "repo_roots:\n  api: ~/src/api\n  web: C:/src/web\n"))
    assert cfg.promote_owners == ["acme", "Contoso"]
    assert cfg.repo_roots == {"api": Path(os.path.expanduser("~/src/api")), "web": Path("C:/src/web")}
    defaults = Config(store_path=Path("x"))
    assert defaults.promote_owners == [] and defaults.repo_roots == {}


@pytest.mark.parametrize("text", ["promote_owners: acme\n", "promote_owners: [1]\n", "repo_roots: [a]\n",
                                  "repo_roots: {api: 5}\n", "promote_owners: ['']\n"])
def test_promote_owners_and_repo_roots_type_checked(monkeypatch, tmp_path, text):
    monkeypatch.delenv("SHARED_CONTEXT_STORE", raising=False)
    with pytest.raises(ValueError):
        load_config(_cfg_file(tmp_path, text))
