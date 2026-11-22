"""Unit conversion tools: lengths and temperatures."""

LENGTH_IN_METRES = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "mi": 1609.344,
}
TEMPERATURE_UNITS = ("C", "F", "K")


def convert_length(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a length between mm, cm, m, km, in, ft and mi (6 decimals)."""
    for unit in (from_unit, to_unit):
        if unit not in LENGTH_IN_METRES:
            raise ValueError(f"unknown length unit {unit!r}")
    metres = value * LENGTH_IN_METRES[from_unit]
    return round(metres / LENGTH_IN_METRES[to_unit], 6)


def convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a temperature between C, F and K (2 decimals)."""
    for unit in (from_unit, to_unit):
        if unit not in TEMPERATURE_UNITS:
            raise ValueError(f"unknown temperature unit {unit!r}")
    celsius = value
    if from_unit == "F":
        celsius = (value - 32) * 5 / 9
    elif from_unit == "K":
        celsius = value - 273.15
    result = celsius
    if to_unit == "F":
        result = celsius * 9 / 5 + 32
    elif to_unit == "K":
        result = celsius + 273.15
    return round(result, 2)
