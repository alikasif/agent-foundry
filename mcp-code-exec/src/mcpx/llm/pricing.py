"""Price table lookup for benchmarked models."""

import tomllib
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Prices:
    """USD prices per million tokens for one model."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None


def _load() -> dict[str, dict[str, float]]:
    """Read ``prices.toml`` from the package data."""
    text = resources.files("mcpx.llm").joinpath("prices.toml").read_text("utf-8")
    return tomllib.loads(text)


def price_for(model_id: str) -> Prices:
    """Return the prices for ``model_id``.

    Raises:
        KeyError: If the model is not in ``prices.toml``; the message lists the
            known model ids.
    """
    table = _load()
    entry = table.get(model_id)
    if entry is None:
        known = ", ".join(sorted(table)) or "none"
        raise KeyError(
            f"No prices for model {model_id!r} in prices.toml; known models: {known}"
        )
    return Prices(
        input_per_mtok=float(entry["input_per_mtok"]),
        output_per_mtok=float(entry["output_per_mtok"]),
        cache_read_per_mtok=(
            float(entry["cache_read_per_mtok"])
            if "cache_read_per_mtok" in entry
            else None
        ),
    )
