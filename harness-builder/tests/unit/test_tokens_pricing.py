"""Token counters and the model price table (FR-009 inputs, FR-015, U10)."""

import subprocess
import sys
from pathlib import Path

import pytest

from harness_builder.budget.ledger import PriceLookup
from harness_builder.core.protocols import TokenCounter
from harness_builder.core.types import Message, ToolCall
from harness_builder.llm.pricing import (
    ModelPrice,
    PriceTable,
    candidate_keys,
    litellm_bundled_price,
    load_prices,
)
from harness_builder.llm.tokens import HeuristicCounter, LiteLlmCounter, message_chars

CHEAP = ModelPrice(input_cost_per_token=1e-6, output_cost_per_token=2e-6)
DEAR = ModelPrice(input_cost_per_token=1.0, output_cost_per_token=2.0)


# --- HeuristicCounter --------------------------------------------------------


def test_heuristic_counter_satisfies_protocol() -> None:
    counter: TokenCounter = HeuristicCounter()
    assert counter.count([]) == 0


@pytest.mark.parametrize(
    ("text", "tokens"), [("", 0), ("a", 1), ("abcd", 1), ("abcde", 2), ("x" * 8, 2)]
)
def test_count_text_rounds_up(text: str, tokens: int) -> None:
    assert HeuristicCounter().count_text(text) == tokens


def test_message_chars_include_tool_call_json() -> None:
    call = ToolCall(id="c", name="add", args={"b": 2, "a": 1})
    msg = Message(role="assistant", content="hi", tool_calls=[call])
    assert message_chars(msg) == 2 + 3 + len('{"a": 1, "b": 2}')


def test_count_rounds_up_per_message() -> None:
    msgs = [Message(role="user", content="a"), Message(role="user", content="b")]
    assert HeuristicCounter().count(msgs) == 2


def test_count_ignores_role() -> None:
    counter = HeuristicCounter()
    sys_msg = [Message(role="system", content="abcdefgh")]
    user_msg = [Message(role="user", content="abcdefgh")]
    assert counter.count(sys_msg) == counter.count(user_msg) == 2


def test_litellm_counter_construction_does_not_import_litellm() -> None:
    code = (
        "import sys\n"
        "from harness_builder.llm.tokens import LiteLlmCounter\n"
        "c = LiteLlmCounter('openai/gpt-4o')\n"
        "print('litellm' in sys.modules, c.model)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.split() == ["False", "openai/gpt-4o"]
    assert LiteLlmCounter("m").model == "m"


# --- ModelPrice / candidate_keys --------------------------------------------


def test_model_price_cost() -> None:
    assert CHEAP.cost(1000, 500) == pytest.approx(1000 * 1e-6 + 500 * 2e-6)
    assert CHEAP.cost(0, 0) == 0


def test_candidate_keys_cover_prefix_provider_and_dots() -> None:
    keys = candidate_keys("openrouter/anthropic/claude-sonnet-4.5")
    assert keys[0] == "openrouter/anthropic/claude-sonnet-4.5"
    for expected in (
        "anthropic/claude-sonnet-4.5",
        "claude-sonnet-4.5",
        "claude-sonnet-4-5",
        "anthropic/claude-sonnet-4-5",
    ):
        assert expected in keys
    assert len(keys) == len(set(keys))


def test_candidate_keys_add_openrouter_prefix() -> None:
    assert "openrouter/openai/gpt-4o" in candidate_keys("openai/gpt-4o")


# --- PriceTable --------------------------------------------------------------


def test_package_yaml_prices_known_models() -> None:
    table = PriceTable(fallback=None)
    price = table.price("anthropic/claude-sonnet-4.5")
    assert price is not None
    assert price.input_cost_per_token > 0
    assert table.price("openrouter/anthropic/claude-sonnet-4.5") == price


def test_overrides_win_over_package() -> None:
    table = PriceTable(overrides={"openai/gpt-4o": DEAR}, fallback=None)
    assert table.price("openai/gpt-4o") == DEAR
    assert table.price("openrouter/openai/gpt-4o") == DEAR


def test_package_argument_replaces_yaml() -> None:
    table = PriceTable(package={"my/model": CHEAP}, fallback=None)
    assert table.price("my/model") == CHEAP
    assert table.price("anthropic/claude-sonnet-4.5") is None


def test_unknown_model_without_fallback_is_none() -> None:
    assert PriceTable(fallback=None).price("nobody/knows-this") is None


def test_fallback_only_called_when_tables_miss() -> None:
    seen: list[str] = []

    def spy(model: str) -> ModelPrice | None:
        seen.append(model)
        return CHEAP

    table = PriceTable(overrides={"o/m": DEAR}, package={"p/m": DEAR}, fallback=spy)
    assert table.price("o/m") == DEAR
    assert table.price("p/m") == DEAR
    assert seen == []
    assert table.price("x/unknown") == CHEAP
    assert seen == ["x/unknown"]


def test_bundled_litellm_price_is_offline_and_positive() -> None:
    price = litellm_bundled_price("openai/gpt-4o")
    assert price is not None
    assert price.input_cost_per_token > 0
    assert price.output_cost_per_token > 0


def test_bundled_litellm_price_unknown_is_none() -> None:
    assert litellm_bundled_price("nobody/never-a-model-xyz") is None


def test_default_table_uses_bundled_fallback() -> None:
    table = PriceTable(package={})
    assert table.price("openai/gpt-4o") is not None


def test_load_prices_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "prices.yaml"
    path.write_text(
        "my/model:\n  input_cost_per_token: 1.0e-6\n  output_cost_per_token: 2.0e-6\n",
        encoding="utf-8",
    )
    assert load_prices(path) == {"my/model": CHEAP}


def test_load_prices_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert load_prices(path) == {}


def test_load_prices_rejects_bad_entry(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("m:\n  input_cost_per_token: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_prices(path)


def test_price_table_fits_ledger_price_lookup() -> None:
    lookup: PriceLookup = PriceTable(fallback=None).price
    price = lookup("openai/gpt-4o")
    assert price is not None
    assert price.output_cost_per_token > price.input_cost_per_token
