from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .launch_config import same_day_label
from .location_utils import UserLocation, cities_match, normalize_city
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
    """True when customer and branch are in the same city (IDs or normalized names)."""
    if branch.city_ref_id and getattr(location, "city_id", None):
        return branch.city_ref_id == location.city_id
    branch_city = (branch.city or "").strip()
    customer_city = (getattr(location, "city", None) or "").strip()
    if not branch_city or not customer_city:
        return False
    return cities_match(branch_city, customer_city) or (
        normalize_city(branch_city) == normalize_city(customer_city)
    )


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
    same_country = bool(location and _same_country(branch, location))
    same_city = bool(location and _same_city(branch, location))
    same_day_ok = settings.same_day_enabled and same_city
    branch_city_name = (branch.city or "").strip() or (
        branch.city_ref.name if branch.city_ref_id and branch.city_ref else ""
    )

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
            label=same_day_label(branch_city_name),
            fee=settings.same_day_fee,
            max_delivery_hours=settings.same_day_max_delivery_hours,
            available=same_day_ok,
            reason=""
            if same_day_ok
            else (
                "Same-day delivery disabled"
                if not settings.same_day_enabled
                else "Same-day delivery is only available in the store's city"
            ),
        )
    )
    nationwide_ok = settings.nationwide_enabled and same_country
    options.append(
        DeliveryOption(
            fulfillment_type=Order.FulfillmentType.NATIONWIDE,
            label="Nationwide / standard delivery",
            fee=settings.nationwide_delivery_fee,
            max_delivery_hours=settings.nationwide_max_delivery_hours,
            available=nationwide_ok,
            reason=""
            if nationwide_ok
            else (
                "Nationwide delivery disabled"
                if not settings.nationwide_enabled
                else "Outside delivery country"
            ),
        )
    )
    return options


def option_to_dict(option: DeliveryOption) -> dict:
    data = asdict(option)
    data["fee"] = str(option.fee)
    return data


def compute_promised_by(max_delivery_hours: int | None):
    if max_delivery_hours is None:
        return None
    return timezone.now() + timedelta(hours=max_delivery_hours)
