from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .location_utils import UserLocation, normalize_city
from .models import Branch, BranchFulfillmentSettings, Order
from .visibility import is_near_branch


@dataclass(frozen=True)
class DeliveryOption:
    fulfillment_type: str
    label: str
    fee: Decimal
    max_delivery_hours: int | None
    available: bool
    reason: str = ""


def get_or_create_fulfillment_settings(branch: Branch) -> BranchFulfillmentSettings:
    settings, _ = BranchFulfillmentSettings.objects.get_or_create(branch=branch)
    return settings


def _same_city(branch: Branch, location: UserLocation) -> bool:
    if branch.city_ref_id and getattr(location, "city_id", None):
        return branch.city_ref_id == location.city_id
    return normalize_city(branch.city) == location.city_normalized


def _same_country(branch: Branch, location: UserLocation) -> bool:
    branch_country_id = None
    if branch.city_ref_id and branch.city_ref:
        branch_country_id = branch.city_ref.country_id
    elif branch.business.primary_country_id:
        branch_country_id = branch.business.primary_country_id
    user_country_id = getattr(location, "country_id", None)
    if branch_country_id and user_country_id:
        return branch_country_id == user_country_id
    # Default: assume same country when IDs missing (single-country launch).
    return True


def resolve_delivery_options(
    branch: Branch, location: UserLocation | None
) -> list[DeliveryOption]:
    settings = get_or_create_fulfillment_settings(branch)
    options: list[DeliveryOption] = []

    near = bool(location and is_near_branch(branch, location))
    same_city = bool(location and _same_city(branch, location))
    same_country = bool(location and _same_country(branch, location))

    options.append(
        DeliveryOption(
            fulfillment_type=Order.FulfillmentType.PICKUP,
            label="In-store pickup",
            fee=Decimal("0.00"),
            max_delivery_hours=None,
            available=settings.pickup_enabled and near,
            reason=""
            if settings.pickup_enabled and near
            else (
                "Pickup disabled"
                if not settings.pickup_enabled
                else "Too far for pickup"
            ),
        )
    )
    options.append(
        DeliveryOption(
            fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
            label="Local / same-day delivery",
            fee=settings.local_delivery_fee,
            max_delivery_hours=settings.local_max_delivery_hours,
            available=settings.local_same_day_enabled and same_city,
            reason=""
            if settings.local_same_day_enabled and same_city
            else (
                "Local delivery disabled"
                if not settings.local_same_day_enabled
                else "Available only in the branch city"
            ),
        )
    )
    options.append(
        DeliveryOption(
            fulfillment_type=Order.FulfillmentType.NATIONWIDE,
            label="Nationwide / standard delivery",
            fee=settings.nationwide_delivery_fee,
            max_delivery_hours=settings.nationwide_max_delivery_hours,
            available=settings.nationwide_enabled and same_country and not same_city,
            reason=""
            if settings.nationwide_enabled and same_country and not same_city
            else (
                "Nationwide delivery disabled"
                if not settings.nationwide_enabled
                else (
                    "Use local delivery in this city"
                    if same_city
                    else "Outside delivery country"
                )
            ),
        )
    )
    # If nationwide is enabled and local is unavailable, allow nationwide even in same city.
    if settings.nationwide_enabled and same_country and same_city:
        if not settings.local_same_day_enabled:
            options = [
                opt
                if opt.fulfillment_type != Order.FulfillmentType.NATIONWIDE
                else DeliveryOption(
                    fulfillment_type=opt.fulfillment_type,
                    label=opt.label,
                    fee=opt.fee,
                    max_delivery_hours=opt.max_delivery_hours,
                    available=True,
                    reason="",
                )
                for opt in options
            ]
    return options


def option_to_dict(option: DeliveryOption) -> dict:
    data = asdict(option)
    data["fee"] = str(option.fee)
    return data


def compute_promised_by(max_delivery_hours: int | None):
    if max_delivery_hours is None:
        return None
    return timezone.now() + timedelta(hours=max_delivery_hours)
