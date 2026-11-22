"""T056 review fixes: spec-level promotion (MJ1) and the replace_path home (MJ3)."""

from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.executor.fake import FakeExecutor
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.skills import promotion, store
from harness_builder.skills.extension import install_skills
from harness_builder.skills.promotion import PromotionError, _DeferredLLM
from harness_builder.spec.io import parse_spec
from harness_builder.spec.models import PromotionSpec, SkillsConfig

PINNED_HASH_WITHOUT_PROMOTION = (
    "e19847a2e9f5a1909609ec063210c3059d6fddac613db922fc83ddf89f03185c"
)
"""``config_hash`` of a harness with the skills extension and no promotion,
computed on a ``git archive`` of c1a0cff (before ``SkillsConfig.promotion``)."""


def build(tmp_path: Path, **skills: object) -> Harness:
    builder = HarnessBuilder("t", Settings.from_env())
    builder.model("fake/model").llm(FakeLLMClient([])).executor(FakeExecutor())
    install_skills(SkillsConfig(root=str(tmp_path), **skills), builder)  # pyrefly: ignore[bad-argument-type]
    return builder.build()


def test_without_promotion_no_promotion_hook_is_installed(tmp_path: Path) -> None:
    harness = build(tmp_path)
    assert "skills:promotion" not in harness.config.hooks["post_tool"]
    assert "on_stop" not in harness.config.hooks


def test_spec_promotion_defaults_to_deferred_and_installs_the_hook(
    tmp_path: Path,
) -> None:
    assert PromotionSpec().mode == "deferred"
    harness = build(tmp_path, promotion=PromotionSpec())
    assert "skills:promotion" in harness.config.hooks["on_stop"]
    assert "skills:promotion" in harness.config.hooks["post_tool"]
    [contributor] = harness.config.prompt_contributors
    assert contributor["options"]["promotion_mode"] == "deferred"


@pytest.mark.parametrize("promotion_data", [{"mode": "on_stop"}, {"x": 1}])
def test_spec_rejects_on_stop_and_unknown_promotion_keys(
    promotion_data: dict[str, object],
) -> None:
    data = {
        "schema_version": 1,
        "name": "t",
        "skills": {"promotion": promotion_data},
    }
    with pytest.raises(Exception, match="promotion"):
        parse_spec(data, "harness.yaml")


def test_spec_parses_an_empty_promotion_section() -> None:
    data = {"schema_version": 1, "name": "t", "skills": {"promotion": {}}}
    spec = parse_spec(data, "harness.yaml")
    assert spec.skills is not None and spec.skills.promotion == PromotionSpec()


def test_config_hash_is_unchanged_for_specs_without_promotion(
    tmp_path: Path,
) -> None:
    assert build(tmp_path).config_hash == PINNED_HASH_WITHOUT_PROMOTION


@pytest.mark.anyio
async def test_deferred_hook_llm_refuses_to_generate() -> None:
    with pytest.raises(PromotionError):
        await _DeferredLLM().complete(None)  # pyrefly: ignore[bad-argument-type]


def test_replace_path_lives_in_the_store_and_promotion_reexports_it() -> None:
    assert promotion.replace_path is store.replace_path
    assert promotion.REPLACE_RETRY_DELAYS_S is store.REPLACE_RETRY_DELAYS_S
