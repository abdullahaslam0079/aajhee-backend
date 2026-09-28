"""Launch / coverage cities for Aajhee.

Edit ENABLED_CITIES to add Karachi, Islamabad, etc. This is the single
source of truth for which cities the platform currently serves (map bias,
default branch city, address hints). Same-day delivery itself uses a
generic same-city rule and does not hardcode city names.
"""

from __future__ import annotations

from .location_utils import normalize_city

# Add cities here as Aajhee expands beyond the first launch market.
ENABLED_CITIES: tuple[dict, ...] = (
    {
        "name": "Lahore",
        "latitude": 31.5204,
        "longitude": 74.3587,
    },
)


def enabled_city_names() -> list[str]:
    return [c["name"] for c in ENABLED_CITIES]


def primary_city() -> dict:
    """First enabled city — used for map defaults and form placeholders."""
    return ENABLED_CITIES[0]


def is_enabled_city(name: str | None) -> bool:
    if not name:
        return False
    target = normalize_city(name)
    return any(normalize_city(c["name"]) == target for c in ENABLED_CITIES)


def same_day_label(city: str | None) -> str:
    cleaned = (city or "").strip()
    if cleaned:
        return f"Same-day delivery in {cleaned}"
    return "Same-day delivery"
