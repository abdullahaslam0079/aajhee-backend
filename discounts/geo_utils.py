from __future__ import annotations

from .location_utils import normalize_city
from .models import City, Country


DEFAULT_COUNTRY_CODE = "PK"
DEFAULT_COUNTRY_NAME = "Pakistan"


def get_or_create_default_country() -> Country:
    country, _ = Country.objects.get_or_create(
        code=DEFAULT_COUNTRY_CODE,
        defaults={"name": DEFAULT_COUNTRY_NAME},
    )
    return country


def get_or_create_city(
    name: str, *, country: Country | None = None
) -> City | None:
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    if country is None:
        country = get_or_create_default_country()
    normalized = normalize_city(cleaned)
    city, _ = City.objects.get_or_create(
        country=country,
        name_normalized=normalized,
        defaults={"name": cleaned},
    )
    return city
