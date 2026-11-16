"""Model price table (designs.md 5, FR-009, U10).

Layering: user overrides win over the package ``pricing.yaml``, and the litellm
cost map is consulted only when neither knows the model. The default fallback
reads litellm's *bundled* cost map JSON from disk without importing litellm,
because ``import litellm`` fetches a remote map (network, non-deterministic).
Prices are USD per token, matching ``litellm.model_cost`` and the structural
``budget.ledger.ModelPrice`` protocol.
"""

import functools
import importlib.resources
import importlib.util
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

OPENROUTER_PREFIX = "openrouter/"
LITELLM_COST_MAP_FILE = "model_prices_and_context_window_backup.json"


class ModelPrice(BaseModel):
    """Per-token USD prices of one model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_cost_per_token: float
    output_cost_per_token: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Return the USD cost of the given token counts."""
        return (
            input_tokens * self.input_cost_per_token
            + output_tokens * self.output_cost_per_token
        )


PriceFallback = Callable[[str], ModelPrice | None]


def candidate_keys(model: str) -> list[str]:
    """Return lookup keys for ``model``, most specific first (heuristic).

    Tries the name as given, with and without the ``openrouter/`` prefix,
    without the provider segment, and each with dots replaced by dashes
    (OpenRouter ``claude-sonnet-4.5`` vs litellm ``claude-sonnet-4-5``).
    """
    bare = model.removeprefix(OPENROUTER_PREFIX)
    names = [model, bare, OPENROUTER_PREFIX + bare, bare.split("/", 1)[-1]]
    keys: list[str] = []
    for name in names + [n.replace(".", "-") for n in names]:
        if name not in keys:
            keys.append(name)
    return keys


def load_prices(path: Path) -> dict[str, ModelPrice]:
    """Read a ``{model: {input_cost_per_token, output_cost_per_token}}`` YAML."""
    return _parse_prices(yaml.safe_load(path.read_text(encoding="utf-8")))


def _parse_prices(raw: Mapping[str, Any] | None) -> dict[str, ModelPrice]:
    return {name: ModelPrice.model_validate(v) for name, v in (raw or {}).items()}


@functools.cache
def _package_prices() -> dict[str, ModelPrice]:
    text = (
        importlib.resources.files("harness_builder.llm")
        .joinpath("pricing.yaml")
        .read_text(encoding="utf-8")
    )
    return _parse_prices(yaml.safe_load(text))


@functools.cache
def _litellm_cost_map() -> dict[str, Any]:
    spec = importlib.util.find_spec("litellm")
    if spec is None or spec.origin is None:
        return {}
    path = Path(spec.origin).parent / LITELLM_COST_MAP_FILE
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def litellm_bundled_price(model: str) -> ModelPrice | None:
    """Look ``model`` up in litellm's bundled cost map (offline, no import)."""
    cost_map = _litellm_cost_map()
    for key in candidate_keys(model):
        entry = cost_map.get(key)
        if not isinstance(entry, dict):
            continue
        input_cost = entry.get("input_cost_per_token")
        output_cost = entry.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue
        return ModelPrice(
            input_cost_per_token=input_cost, output_cost_per_token=output_cost
        )
    return None


class PriceTable:
    """Resolves model prices: overrides, then package yaml, then fallback.

    ``price`` matches the ledger's ``PriceLookup`` and returns None for an
    unknown model (the builder turns that into ``ConfigError`` when a cost
    cap is set, U10). Pass ``fallback=None`` to disable the litellm lookup.
    """

    def __init__(
        self,
        overrides: Mapping[str, ModelPrice] | None = None,
        fallback: PriceFallback | None = litellm_bundled_price,
        package: Mapping[str, ModelPrice] | None = None,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._package = dict(_package_prices() if package is None else package)
        self._fallback = fallback

    def price(self, model: str) -> ModelPrice | None:
        """Return the price of ``model`` or None when no source knows it."""
        for table in (self._overrides, self._package):
            for key in candidate_keys(model):
                if key in table:
                    return table[key]
        return self._fallback(model) if self._fallback is not None else None
