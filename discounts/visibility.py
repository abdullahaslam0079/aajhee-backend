from __future__ import annotations

from dataclasses import dataclass

from .location_utils import UserLocation, branch_distance_km, normalize_city
from .models import Branch, BranchFulfillmentSettings, Business


@dataclass(frozen=True)
class ChannelVisibility:
    show_online: bool
    show_instore: bool
    nearest_branch_id: int | None = None
    nearest_distance_km: float | None = None


def _branch_pickup_radius_km(branch: Branch) -> float:
    try:
        settings = branch.fulfillment_settings
    except BranchFulfillmentSettings.DoesNotExist:
        return 15.0
    return float(settings.pickup_radius_km)


def is_near_branch(branch: Branch, location: UserLocation) -> bool:
    same_city = False
    if branch.city_ref_id and getattr(location, "city_id", None):
        same_city = branch.city_ref_id == location.city_id
    elif branch.city:
        same_city = normalize_city(branch.city) == location.city_normalized
    if same_city:
        return True
    distance = branch_distance_km(branch, location)
    return distance <= _branch_pickup_radius_km(branch)


def resolve_business_visibility(
    business: Business,
    location: UserLocation | None,
    *,
    branches: list[Branch] | None = None,
) -> ChannelVisibility:
    branch_list = branches if branches is not None else list(business.branches.all())
    nearest_id: int | None = None
    nearest_km: float | None = None
    near_any = False

    if location is not None and branch_list:
        scored = [
            (branch_distance_km(branch, location), branch) for branch in branch_list
        ]
        scored.sort(key=lambda item: item[0])
        nearest_km, nearest_branch = scored[0]
        nearest_id = nearest_branch.id
        near_any = any(is_near_branch(branch, location) for branch in branch_list)

    mode = business.presence_mode
    coverage = business.online_coverage

    if mode == Business.PresenceMode.ONLINE_ONLY:
        show_online = True
        if coverage == Business.OnlineCoverage.CITY and location is not None:
            if business.primary_city_id and getattr(location, "city_id", None):
                show_online = business.primary_city_id == location.city_id
            elif business.primary_city:
                show_online = (
                    normalize_city(business.primary_city.name)
                    == location.city_normalized
                )
        return ChannelVisibility(
            show_online=show_online,
            show_instore=False,
            nearest_branch_id=nearest_id,
            nearest_distance_km=nearest_km,
        )

    if mode == Business.PresenceMode.INSTORE_ONLY:
        return ChannelVisibility(
            show_online=False,
            show_instore=near_any,
            nearest_branch_id=nearest_id if near_any else None,
            nearest_distance_km=nearest_km if near_any else None,
        )

    # hybrid
    show_online = True
    if coverage == Business.OnlineCoverage.CITY and location is not None:
        if business.primary_city_id and getattr(location, "city_id", None):
            show_online = business.primary_city_id == location.city_id
        elif business.primary_city:
            show_online = (
                normalize_city(business.primary_city.name) == location.city_normalized
            )
        else:
            # Fall back to any branch city match
            show_online = any(
                (
                    branch.city_ref_id == getattr(location, "city_id", None)
                    if branch.city_ref_id and getattr(location, "city_id", None)
                    else normalize_city(branch.city) == location.city_normalized
                )
                for branch in branch_list
            ) or near_any

    return ChannelVisibility(
        show_online=show_online,
        show_instore=near_any,
        nearest_branch_id=nearest_id if near_any else nearest_id,
        nearest_distance_km=nearest_km,
    )


def business_is_visible(
    business: Business,
    location: UserLocation | None,
    *,
    branches: list[Branch] | None = None,
) -> bool:
    channels = resolve_business_visibility(business, location, branches=branches)
    return channels.show_online or channels.show_instore
