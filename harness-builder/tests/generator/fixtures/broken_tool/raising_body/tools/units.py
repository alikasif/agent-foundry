"""Unit conversion tools (broken: a body that always raises)."""

LENGTH_IN_METRES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0}


def convert_length(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a length; the keys are looked up upper-cased, so it raises."""
    factor = LENGTH_IN_METRES[from_unit.upper()]
    return round(value * factor / LENGTH_IN_METRES[to_unit.upper()], 6)


def convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a temperature between C and F only."""
    if from_unit == to_unit:
        return value
    if from_unit == "C":
        return round(value * 9 / 5 + 32, 2)
    return round((value - 32) * 5 / 9, 2)
