"""Moved here by ADR 0026 (module order)."""

from collections.abc import Mapping

# Customer spellings -> catalogue unit. Only exact aliases; e.g. "box" is never "each".
UNIT_ALIASES: Mapping[str, str] = {
    "each": "each", "ea": "each", "pc": "each", "pcs": "each", "piece": "each",
    "pieces": "each", "unit": "each", "units": "each", "no": "each", "nos": "each",
    "m": "metre", "metre": "metre", "metres": "metre", "meter": "metre", "meters": "metre",
    "mtr": "metre", "mtrs": "metre",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "box": "box", "boxes": "box",
}  # fmt: skip


def normalise_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    return UNIT_ALIASES.get(unit.strip().casefold().rstrip("."))
