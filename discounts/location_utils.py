from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt

from django.db.models import Case, IntegerField, QuerySet, When
from rest_framework.exceptions import NotFound, ValidationError

from .address_utils import get_user_address
from .models import Branch, Offer


EARTH_RADIUS_KM = 6371.0
FALLBACK_RADIUS_KM = 10.0
DEFAULT_MAP_NEARBY_RADIUS_KM = 4.0
MIN_MAP_NEARBY_RADIUS_KM = 0.5
MAX_MAP_NEARBY_RADIUS_KM = 25.0


@dataclass(frozen=True)
class UserLocation:
    latitude: Decimal
    longitude: Decimal
    city: str
    city_id: int | None = None
    country_id: int | None = None

    @property
    def city_normalized(self) -> str:
        return normalize_city(self.city)


def normalize_city(city: str) -> str:
    return city.strip().casefold()


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    lat1_f, lon1_f, lat2_f, lon2_f = map(float, [lat1, lon1, lat2, lon2])
    dlat = radians(lat2_f - lat1_f)
    dlon = radians(lon2_f - lon1_f)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1_f)) * cos(radians(lat2_f)) * sin(
        dlon / 2
    ) ** 2
    return EARTH_RADIUS_KM * 2 * asin(sqrt(a))


def branch_distance_km(branch: Branch, location: UserLocation) -> float:
    return haversine_km(
        location.latitude,
        location.longitude,
        branch.latitude,
        branch.longitude,
    )


def resolve_user_location(request) -> UserLocation | None:
    params = request.query_params

    lat = params.get("latitude")
    lon = params.get("longitude")
    city = params.get("city")
    city_id = params.get("city_id")
    country_id = params.get("country_id")
    if lat is not None and lon is not None and city and str(city).strip():
        try:
            return UserLocation(
                latitude=Decimal(str(lat)),
                longitude=Decimal(str(lon)),
                city=str(city).strip(),
                city_id=int(city_id) if city_id else None,
                country_id=int(country_id) if country_id else None,
            )
        except Exception:
            return None

    user = request.user
    if not user.is_authenticated:
        return None

    address_id = params.get("address_id")
    if address_id:
        try:
            address = get_user_address(user, address_id)
        except NotFound:
            address = None
        if address is not None:
            return UserLocation(
                latitude=address.latitude,
                longitude=address.longitude,
                city=address.city,
                city_id=address.city_ref_id,
                country_id=(
                    address.city_ref.country_id if address.city_ref_id else None
                ),
            )

    address = user.addresses.filter(is_default=True).first()
    if address is None:
        address = user.addresses.order_by("id").first()
    if address is None:
        return None

    return UserLocation(
        latitude=address.latitude,
        longitude=address.longitude,
        city=address.city,
        city_id=address.city_ref_id,
        country_id=address.city_ref.country_id if address.city_ref_id else None,
    )


def resolve_map_search_center(request) -> UserLocation | None:
    """
    Map Discover search center. Explicit lat/lng wins so panning the map
    can fetch a new area without changing the user's saved address.
    Falls back to address_id / default address when coordinates are omitted.
    """
    params = request.query_params
    lat = params.get("latitude")
    lon = params.get("longitude")
    if lat is not None or lon is not None:
        if lat is None or lon is None:
            raise ValidationError(
                {"detail": "latitude and longitude are required together."}
            )
        try:
            latitude = Decimal(str(lat))
            longitude = Decimal(str(lon))
        except Exception as exc:
            raise ValidationError(
                {"detail": "latitude and longitude must be valid numbers."}
            ) from exc
        if not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
            raise ValidationError(
                {"detail": "latitude or longitude is out of range."}
            )
        return UserLocation(
            latitude=latitude,
            longitude=longitude,
            city=str(params.get("city") or "").strip(),
        )
    return resolve_user_location(request)


def parse_map_radius_km(request) -> float:
    raw = request.query_params.get("radius_km")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAP_NEARBY_RADIUS_KM
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"detail": "radius_km must be a number."}) from exc
    return min(max(value, MIN_MAP_NEARBY_RADIUS_KM), MAX_MAP_NEARBY_RADIUS_KM)


def branches_in_city(branches: list[Branch], location: UserLocation) -> list[Branch]:
    return [
        branch
        for branch in branches
        if normalize_city(branch.city) == location.city_normalized
    ]


def branches_within_radius(
    branches: list[Branch], location: UserLocation, radius_km: float
) -> list[Branch]:
    return [
        branch
        for branch in branches
        if branch_distance_km(branch, location) <= radius_km
    ]


def sort_branches_by_distance(
    branches: list[Branch], location: UserLocation
) -> list[Branch]:
    return sorted(branches, key=lambda branch: branch_distance_km(branch, location))


def resolve_location_branch_scope(
    branches: list[Branch], location: UserLocation
) -> tuple[list[int], str]:
    """
    Pick branches in the user's city when any exist; otherwise fall back to
    branches within FALLBACK_RADIUS_KM. Returns ids nearest-first and filter mode.
    """
    city_branches = branches_in_city(branches, location)
    if city_branches:
        ordered = sort_branches_by_distance(city_branches, location)
        return [branch.id for branch in ordered], "city"

    nearby = branches_within_radius(branches, location, FALLBACK_RADIUS_KM)
    ordered = sort_branches_by_distance(nearby, location)
    return [branch.id for branch in ordered], "radius"


def order_queryset_by_id_sequence(queryset: QuerySet, ordered_ids: list[int]) -> QuerySet:
    if not ordered_ids:
        return queryset.none()
    ordering = Case(
        *[When(pk=pk, then=position) for position, pk in enumerate(ordered_ids)],
        output_field=IntegerField(),
    )
    return queryset.filter(pk__in=ordered_ids).order_by(ordering)


def filter_branches_for_location(
    queryset: QuerySet, location: UserLocation
) -> tuple[QuerySet, str]:
    branch_list = list(queryset)
    ordered_ids, mode = resolve_location_branch_scope(branch_list, location)
    return order_queryset_by_id_sequence(queryset, ordered_ids), mode


def filter_branches_within_radius(
    queryset: QuerySet, location: UserLocation, radius_km: float
) -> QuerySet:
    nearby = branches_within_radius(list(queryset), location, radius_km)
    ordered = sort_branches_by_distance(nearby, location)
    return order_queryset_by_id_sequence(
        queryset, [branch.id for branch in ordered]
    )


def filter_offers_for_location(
    queryset: QuerySet, location: UserLocation
) -> tuple[QuerySet, str]:
    offer_branch_ids = queryset.values_list("branches__id", flat=True).distinct()
    branches = list(Branch.objects.filter(id__in=offer_branch_ids))
    ordered_branch_ids, mode = resolve_location_branch_scope(branches, location)

    branch_id_set = set(ordered_branch_ids)
    location_offers = (
        list(queryset.filter(branches__id__in=ordered_branch_ids).distinct())
        if ordered_branch_ids
        else []
    )
    online_offers = list(queryset.filter(is_online=True).distinct())

    offers_by_id: dict[int, Offer] = {offer.id: offer for offer in location_offers}
    for offer in online_offers:
        offers_by_id.setdefault(offer.id, offer)

    if not offers_by_id:
        return queryset.none(), mode

    def nearest_distance(offer: Offer) -> float:
        matching = [
            branch for branch in offer.branches.all() if branch.id in branch_id_set
        ]
        if matching:
            return min(branch_distance_km(branch, location) for branch in matching)
        # Online-only (or online with out-of-scope branches) after nearby in-store offers.
        return float("inf")

    ordered_offer_ids = [
        offer.id for offer in sorted(offers_by_id.values(), key=nearest_distance)
    ]
    return order_queryset_by_id_sequence(queryset, ordered_offer_ids), mode


def sort_offers_by_nearest_distance(
    queryset: QuerySet, location: UserLocation
) -> QuerySet:
    """
    Rank all matching offers by nearest linked branch distance.
    Unlike filter_offers_for_location, distant offers are kept (not city/radius scoped).
    Online-only offers (no branches) sort after offers with a known distance.
    """
    offers = list(queryset.distinct())
    if not offers:
        return queryset.none()

    def nearest_distance(offer: Offer) -> float:
        branches = list(offer.branches.all())
        if not branches:
            return float("inf")
        return min(branch_distance_km(branch, location) for branch in branches)

    ordered_offer_ids = [
        offer.id for offer in sorted(offers, key=nearest_distance)
    ]
    return order_queryset_by_id_sequence(queryset, ordered_offer_ids)
