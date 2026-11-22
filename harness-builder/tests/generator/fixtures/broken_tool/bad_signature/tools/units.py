"""Unit conversion tools (broken: an untyped parameter)."""

LENGTH_IN_METRES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0}


def convert_length(value, from_unit: str, to_unit: str) -> float:
    """Convert a length; ``value`` has no type hint, so stage A fails."""
    return round(value * LENGTH_IN_METRES[from_unit] / LENGTH_IN_METRES[to_unit], 6)


def convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a temperature between C and F only."""
    if from_unit == to_unit:
        return value
    if from_unit == "C":
        return round(value * 9 / 5 + 32, 2)
    return round((value - 32) * 5 / 9, 2)
