import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .fields import OptionalImageField
from .models import Branch, Business, Category, Offer, OfferBranchStats, OfferGalleryImage
from .offer_pricing import compute_offer_payment
from .offer_utils import (
    build_media_url,
    build_offer_image_urls,
    can_user_redeem_offer,
)
from .serializers import BranchHighlightSerializer, CategorySerializer

User = get_user_model()


class BusinessRegisterSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=120,
        trim_whitespace=True,
        error_messages={
            "required": "Business name is required.",
            "blank": "Business name cannot be empty.",
        },
    )
    email = serializers.EmailField(
        error_messages={
            "required": "Email is required.",
            "blank": "Email cannot be empty.",
            "invalid": "Enter a valid email address.",
        }
    )
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        error_messages={
            "required": "Password is required.",
            "blank": "Password cannot be empty.",
        },
    )
    password_confirm = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        error_messages={
            "required": "Password confirmation is required.",
            "blank": "Password confirmation cannot be empty.",
        },
    )
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source="category",
        required=True,
        error_messages={
            "required": "Business category is required.",
            "does_not_exist": "Selected category does not exist.",
        },
    )
    logo = OptionalImageField(required=False, allow_null=True)

    def run_validation(self, data=serializers.empty):
        if data is not serializers.empty and hasattr(data, "get"):
            payload = data.copy() if hasattr(data, "copy") else dict(data)
            logo = payload.get("logo")
            if logo in (None, "", b"", [], "null", "none", "undefined"):
                payload.pop("logo", None)
            return super().run_validation(payload)
        return super().run_validation(data)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Business name is required.")
        return value.strip()

    def validate_email(self, value: str) -> str:
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )
        return email

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        email = validated_data.pop("email")
        name = validated_data.pop("name")
        category = validated_data.pop("category")
        logo = validated_data.pop("logo", None)

        user = User.objects.create_user(
            email=email,
            password=password,
            account_type=User.AccountType.BUSINESS,
        )
        business = Business.objects.create(
            owner=user,
            name=name,
            category=category,
            logo=logo,
        )
        business.categories.add(category)
        return business


class BusinessProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="owner.email", read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source="category",
        required=False,
        error_messages={"does_not_exist": "Selected category does not exist."},
    )
    category_name = serializers.CharField(
        source="category.name", read_only=True, allow_null=True
    )
    category = CategorySerializer(read_only=True, allow_null=True)
    logo = OptionalImageField(required=False, allow_null=True, write_only=True)
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Business
        fields = [
            "id",
            "name",
            "email",
            "logo",
            "logo_url",
            "category_id",
            "category_name",
            "category",
        ]
        read_only_fields = ["id", "email", "category_name", "category"]

    def run_validation(self, data=serializers.empty):
        if data is not serializers.empty and hasattr(data, "get"):
            payload = data.copy() if hasattr(data, "copy") else dict(data)
            logo = payload.get("logo")
            if logo in (None, "", b"", [], "null", "none", "undefined"):
                payload.pop("logo", None)
            return super().run_validation(payload)
        return super().run_validation(data)

    def get_logo_url(self, obj: Business) -> str | None:
        return build_media_url(self.context.get("request"), obj.logo)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["category_id"] = instance.category_id
        data["presence_mode"] = instance.presence_mode
        data["online_coverage"] = instance.online_coverage
        data["category_ids"] = list(instance.categories.values_list("id", flat=True))
        data["primary_city_id"] = instance.primary_city_id
        data["primary_country_id"] = instance.primary_country_id
        return data


class BusinessLoginTokenObtainPairSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        "no_active_account": "Invalid email or password.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"] = serializers.EmailField(
            error_messages={
                "required": "Email is required.",
                "blank": "Email cannot be empty.",
                "invalid": "Enter a valid email address.",
            }
        )
        self.fields["password"].error_messages = {
            "required": "Password is required.",
            "blank": "Password cannot be empty.",
        }

    def validate(self, attrs):
        data = super().validate(attrs)
        if not self.user.is_business_account:
            raise serializers.ValidationError(
                {"email": "This account is not registered as a business."}
            )
        if not hasattr(self.user, "business_profile"):
            raise serializers.ValidationError(
                {"email": "Business profile not found for this account."}
            )
        data.pop("refresh", None)
        data["business"] = BusinessProfileSerializer(
            self.user.business_profile, context=self.context
        ).data
        return data


class BranchSerializer(BranchHighlightSerializer):
    formattedAddress = serializers.CharField(source="formatted_address", read_only=True)

    class Meta(BranchHighlightSerializer.Meta):
        fields = [
            "id",
            "name",
            "street",
            "house_number",
            "postal_code",
            "city",
            "latitude",
            "longitude",
            "formattedAddress",
            "business_logo_url",
            "highest_discount_percent",
            "highest_discount_offer",
        ]
        read_only_fields = [
            "business_logo_url",
            "highest_discount_percent",
            "highest_discount_offer",
        ]

    def validate_latitude(self, value):
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_longitude(self, value):
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value

    def create(self, validated_data):
        business = self.context["business"]
        return Branch.objects.create(business=business, **validated_data)


class OfferBranchStatsSerializer(serializers.ModelSerializer):
    branch_id = serializers.IntegerField(source="branch.id", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = OfferBranchStats
        fields = ["branch_id", "branch_name", "scan_count", "avail_count"]


class BusinessOfferSerializer(serializers.ModelSerializer):
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False
    )
    branch_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Branch.objects.none(),
        write_only=True,
        required=False,
        allow_empty=True,
        error_messages={
            "does_not_exist": "One or more selected branches were not found.",
        },
    )
    branches = BranchSerializer(many=True, read_only=True)
    branch_stats = OfferBranchStatsSerializer(many=True, read_only=True)
    qr_code = serializers.UUIDField(read_only=True)
    is_active = serializers.SerializerMethodField()
    category_id = serializers.IntegerField(
        source="business.category.id", read_only=True
    )
    category_name = serializers.CharField(
        source="business.category.name", read_only=True
    )
    category = CategorySerializer(source="business.category", read_only=True)
    view_count = serializers.SerializerMethodField()
    like_count = serializers.SerializerMethodField()
    images = serializers.ListField(
        child=OptionalImageField(allow_null=False),
        required=False,
        allow_empty=True,
        write_only=True,
        help_text=(
            "One or more offer images. In multipart form-data, send multiple "
            "files using the same field name: images."
        ),
    )
    gallery_image_urls = serializers.ListField(
        child=serializers.URLField(max_length=1000),
        required=False,
        allow_empty=True,
        write_only=True,
        help_text=(
            "Remote image URLs for the offer gallery. Prefer this when images "
            "already live on a brand/CDN site. Combined with uploaded images "
            "when both are provided."
        ),
    )
    image_urls = serializers.SerializerMethodField()
    included_items = serializers.ListField(
        child=serializers.CharField(max_length=120, allow_blank=False, trim_whitespace=True),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = Offer
        fields = [
            "id",
            "offer_type",
            "redemption_mode",
            "title",
            "description",
            "detailed_description",
            "external_url",
            "external_url_label",
            "images",
            "gallery_image_urls",
            "image_urls",
            "discount_percent",
            "item_name",
            "included_items",
            "original_price",
            "discounted_price",
            "usage_limit_type",
            "usage_limit_count",
            "is_online",
            "branch_ids",
            "branches",
            "is_enabled",
            "is_time_limited",
            "starts_at",
            "ends_at",
            "qr_code",
            "is_active",
            "category_id",
            "category_name",
            "category",
            "branch_stats",
            "view_count",
            "like_count",
            "created_at",
        ]
        read_only_fields = ["id", "qr_code", "created_at", "image_urls"]

    def run_validation(self, data=serializers.empty):
        if data is not serializers.empty and hasattr(data, "get"):
            payload = data.copy() if hasattr(data, "copy") else dict(data)
            empty_values = (None, "", b"", "null", "none", "undefined")
            if hasattr(data, "getlist"):
                images = [
                    item for item in data.getlist("images") if item not in empty_values
                ]
                if "images" in data:
                    if hasattr(payload, "setlist"):
                        payload.setlist("images", images)
                    else:
                        payload["images"] = images

                if "gallery_image_urls" in data:
                    urls = [
                        item
                        for item in data.getlist("gallery_image_urls")
                        if item not in empty_values
                    ]
                    if hasattr(payload, "setlist"):
                        payload.setlist("gallery_image_urls", urls)
                    else:
                        payload["gallery_image_urls"] = urls
            else:
                if "images" in payload:
                    raw_images = payload.get("images")
                    if raw_images in empty_values:
                        payload["images"] = []
                    elif not isinstance(raw_images, list):
                        payload["images"] = [raw_images]
                    else:
                        payload["images"] = [
                            item for item in raw_images if item not in empty_values
                        ]

                if "gallery_image_urls" in payload:
                    raw_urls = payload.get("gallery_image_urls")
                    if raw_urls in empty_values:
                        payload["gallery_image_urls"] = []
                    elif isinstance(raw_urls, str):
                        # Allow newline/comma-separated string from simple clients.
                        parts = [
                            part.strip()
                            for part in raw_urls.replace(",", "\n").splitlines()
                            if part.strip()
                        ]
                        payload["gallery_image_urls"] = parts
                    elif not isinstance(raw_urls, list):
                        payload["gallery_image_urls"] = [raw_urls]
                    else:
                        payload["gallery_image_urls"] = [
                            item for item in raw_urls if item not in empty_values
                        ]

            if "included_items" in payload:
                payload["included_items"] = self._parse_included_items_payload(
                    payload.get("included_items"),
                    empty_values=empty_values,
                    getlist=getattr(data, "getlist", None),
                )

            return super().run_validation(payload)
        return super().run_validation(data)

    def get_image_urls(self, obj: Offer) -> list[str]:
        return build_offer_image_urls(obj, self.context.get("request"))

    @staticmethod
    def _parse_included_items_payload(raw, *, empty_values, getlist=None):
        if getlist is not None and raw not in (None,):
            listed = [item for item in getlist("included_items") if item not in empty_values]
            if listed:
                raw = listed
        if raw in empty_values:
            return []
        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return Offer.normalize_included_items(parsed)
                except json.JSONDecodeError:
                    pass
            return Offer.normalize_included_items(stripped)
        return Offer.normalize_included_items(raw)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business is not None:
            queryset = business.branches.all()
            branch_field = self.fields["branch_ids"]
            branch_field.queryset = queryset
            branch_field.child_relation.queryset = queryset

    def get_is_active(self, obj: Offer) -> bool:
        return obj.is_active

    def _engagement_stats(self, obj: Offer):
        try:
            return obj.engagement_stats
        except ObjectDoesNotExist:
            return None

    @extend_schema_field(OpenApiTypes.INT)
    def get_view_count(self, obj: Offer) -> int:
        """Total offer opens. Authenticated users count once per calendar day."""
        stats = self._engagement_stats(obj)
        return stats.view_count if stats else 0

    @extend_schema_field(OpenApiTypes.INT)
    def get_like_count(self, obj: Offer) -> int:
        stats = self._engagement_stats(obj)
        return stats.like_count if stats else 0

    def _get_business(self):
        return self.context["business"]

    def _validate_branches_belong_to_business(self, branches):
        business = self._get_business()
        invalid = [branch.pk for branch in branches if branch.business_id != business.pk]
        if invalid:
            raise serializers.ValidationError(
                {"branch_ids": "One or more branches do not belong to your business."}
            )

    def _priced_offer_fields(self, attrs):
        original = attrs.get(
            "original_price", getattr(self.instance, "original_price", None)
        )
        discounted = attrs.get(
            "discounted_price", getattr(self.instance, "discounted_price", None)
        )
        if original is None:
            raise serializers.ValidationError(
                {"original_price": "Original price is required."}
            )
        if discounted is None:
            raise serializers.ValidationError(
                {"discounted_price": "Discounted price is required."}
            )
        if original <= 0:
            raise serializers.ValidationError(
                {"original_price": "Original price must be greater than zero."}
            )
        if discounted < 0:
            raise serializers.ValidationError(
                {"discounted_price": "Discounted price cannot be negative."}
            )
        if discounted >= original:
            raise serializers.ValidationError(
                {"discounted_price": "Discounted price must be less than original price."}
            )
        attrs["original_price"] = original
        attrs["discounted_price"] = discounted
        attrs["discount_percent"] = Offer.compute_discount_percent(
            Decimal(str(original)), Decimal(str(discounted))
        )

    def _deal_price_fields(self, attrs):
        discounted = attrs.get(
            "discounted_price", getattr(self.instance, "discounted_price", None)
        )
        if discounted is None:
            raise serializers.ValidationError(
                {"discounted_price": "Deal price is required."}
            )
        if discounted <= 0:
            raise serializers.ValidationError(
                {"discounted_price": "Deal price must be greater than zero."}
            )
        attrs["original_price"] = None
        attrs["discounted_price"] = discounted
        attrs["discount_percent"] = Decimal("0.00")

    def _apply_default_external_url_label(self, attrs, offer_type: str):
        label = attrs.get("external_url_label")
        if label is None and self.instance is not None:
            label = self.instance.external_url_label
        cleaned = (label or "").strip()
        attrs["external_url_label"] = cleaned or Offer.default_external_url_label(
            offer_type
        )

    def _validate_offer_type_fields(self, attrs):
        offer_type = attrs.get(
            "offer_type", getattr(self.instance, "offer_type", None)
        )

        if offer_type == Offer.OfferType.PERCENTAGE_BILL:
            discount = attrs.get("discount_percent")
            if discount is None and self.instance:
                discount = self.instance.discount_percent
            if discount is None:
                raise serializers.ValidationError(
                    {"discount_percent": "Discount percentage is required."}
                )
            attrs["discount_percent"] = discount
            if discount <= 0 or discount > 100:
                raise serializers.ValidationError(
                    {"discount_percent": "Discount must be between 0.01 and 100."}
                )
            attrs["item_name"] = ""
            attrs["included_items"] = []
            attrs["original_price"] = None
            attrs["discounted_price"] = None
        elif offer_type == Offer.OfferType.ITEM:
            item_name = attrs.get(
                "item_name", getattr(self.instance, "item_name", "")
            )
            if not item_name or not item_name.strip():
                raise serializers.ValidationError(
                    {"item_name": "Item or service name is required."}
                )
            self._priced_offer_fields(attrs)
            attrs["item_name"] = item_name.strip()
            attrs["included_items"] = []
        elif offer_type == Offer.OfferType.DEAL:
            included_items = attrs.get("included_items", serializers.empty)
            if included_items is serializers.empty and self.instance is not None:
                included_items = self.instance.included_items
            items = Offer.normalize_included_items(included_items)
            if len(items) < 2:
                raise serializers.ValidationError(
                    {
                        "included_items": (
                            "Add at least two included items for a deal."
                        )
                    }
                )
            self._deal_price_fields(attrs)
            attrs["included_items"] = items
            attrs["item_name"] = ""

        if offer_type:
            self._apply_default_external_url_label(attrs, offer_type)

    def _validate_usage_limits(self, attrs):
        limit_type = attrs.get(
            "usage_limit_type",
            getattr(self.instance, "usage_limit_type", None),
        )
        count = attrs.get(
            "usage_limit_count",
            getattr(self.instance, "usage_limit_count", 1),
        )

        recurring_types = {
            Offer.UsageLimitType.N_TIMES_PER_WEEK,
            Offer.UsageLimitType.N_TIMES_PER_MONTH,
            Offer.UsageLimitType.N_TIMES_TOTAL,
        }
        if limit_type in recurring_types and count < 1:
            raise serializers.ValidationError(
                {"usage_limit_count": "Usage limit count must be at least 1."}
            )

        if limit_type in {
            Offer.UsageLimitType.ONE_TIME,
            Offer.UsageLimitType.ONCE_PER_WEEK,
            Offer.UsageLimitType.ONCE_PER_MONTH,
        }:
            attrs["usage_limit_count"] = 1

    def _validate_time_limits(self, attrs):
        is_time_limited = attrs.get(
            "is_time_limited",
            getattr(self.instance, "is_time_limited", False),
        )
        if not is_time_limited:
            attrs["starts_at"] = None
            attrs["ends_at"] = None
            return

        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if not starts_at and not ends_at:
            raise serializers.ValidationError(
                {
                    "starts_at": "Start or end time is required for time-limited offers.",
                    "ends_at": "Start or end time is required for time-limited offers.",
                }
            )
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError(
                {"ends_at": "End time must be after start time."}
            )

    def validate(self, attrs):
        branches = attrs.pop("branch_ids", serializers.empty)
        if branches is not serializers.empty:
            self._validate_branches_belong_to_business(branches)
            attrs["_branches"] = list(branches)

        is_online = attrs.get(
            "is_online",
            getattr(self.instance, "is_online", False),
        )
        if "_branches" in attrs:
            effective_branches = attrs["_branches"]
        elif self.instance is not None:
            effective_branches = list(self.instance.branches.all())
        else:
            effective_branches = []

        if not is_online and not effective_branches:
            raise serializers.ValidationError(
                {
                    "branch_ids": (
                        "Select at least one branch, or set is_online to true "
                        "for an online-only offer."
                    ),
                    "is_online": (
                        "Set is_online to true for an online-only offer, or "
                        "select at least one branch."
                    ),
                }
            )

        if self.instance is None and "_branches" not in attrs:
            attrs["_branches"] = []

        self._validate_offer_type_fields(attrs)
        self._validate_usage_limits(attrs)
        self._validate_time_limits(attrs)
        return attrs

    def _set_gallery_images(
        self,
        offer: Offer,
        images: list | None = None,
        gallery_urls: list | None = None,
    ) -> None:
        offer.gallery_images.all().delete()
        sort_order = 0
        for url in gallery_urls or []:
            cleaned = (url or "").strip()
            if not cleaned:
                continue
            OfferGalleryImage.objects.create(
                offer=offer,
                source_url=cleaned,
                sort_order=sort_order,
            )
            sort_order += 1
        for image in images or []:
            OfferGalleryImage.objects.create(
                offer=offer,
                image=image,
                sort_order=sort_order,
            )
            sort_order += 1
        first_local = (
            offer.gallery_images.exclude(image="")
            .exclude(image=None)
            .order_by("sort_order", "id")
            .first()
        )
        offer.image = first_local.image.name if first_local else None
        offer.save(update_fields=["image"])

    def create(self, validated_data):
        validated_data.setdefault(
            "redemption_mode", Offer.RedemptionMode.VIEW_ONLY
        )
        images = validated_data.pop("images", None)
        gallery_urls = validated_data.pop("gallery_image_urls", None)
        branches = validated_data.pop("_branches")
        business = self._get_business()
        offer = Offer.objects.create(business=business, **validated_data)
        offer.branches.set(branches)
        if images is not None or gallery_urls is not None:
            self._set_gallery_images(
                offer,
                images=images,
                gallery_urls=gallery_urls,
            )
        return offer

    def update(self, instance, validated_data):
        images = validated_data.pop("images", None)
        gallery_urls = validated_data.pop("gallery_image_urls", None)
        branches = validated_data.pop("_branches", None)
        offer = super().update(instance, validated_data)
        if branches is not None:
            offer.branches.set(branches)
        if images is not None or gallery_urls is not None:
            self._set_gallery_images(
                offer,
                images=images,
                gallery_urls=gallery_urls,
            )
        return offer


class OfferScanSerializer(serializers.Serializer):
    branch_id = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(),
        source="branch",
        error_messages={
            "required": "Branch is required.",
            "does_not_exist": "Branch not found.",
        },
    )
    qr_code = serializers.UUIDField(
        error_messages={
            "required": "QR code is required.",
            "invalid": "Enter a valid QR code.",
        }
    )
    bill_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        error_messages={
            "invalid": "Enter a valid bill amount.",
        },
    )

    def validate(self, attrs):
        offer = self.context["offer"]
        if offer.redemption_mode != Offer.RedemptionMode.SCANNABLE:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "This offer is view-only and cannot be scanned or redeemed."
                    ]
                }
            )

        branch = attrs["branch"]
        qr_code = attrs["qr_code"]
        bill_amount = attrs.get("bill_amount")

        if offer.qr_code != qr_code:
            raise serializers.ValidationError({"qr_code": "Invalid QR code for this offer."})

        if not offer.is_active:
            raise serializers.ValidationError({"qr_code": "This offer is not currently active."})

        if not offer.branches.filter(pk=branch.pk).exists():
            raise serializers.ValidationError(
                {"branch_id": "This offer is not available at the selected branch."}
            )

        if offer.offer_type == Offer.OfferType.PERCENTAGE_BILL:
            if bill_amount is None:
                raise serializers.ValidationError(
                    {"bill_amount": "Bill amount is required for percentage discounts."}
                )
            if bill_amount <= 0:
                raise serializers.ValidationError(
                    {"bill_amount": "Bill amount must be greater than zero."}
                )
        elif bill_amount is not None:
            raise serializers.ValidationError(
                {"bill_amount": "Bill amount is only used for percentage discounts."}
            )

        payment = compute_offer_payment(offer, bill_amount=bill_amount)
        if payment.amount_to_pay is None:
            raise serializers.ValidationError(
                {"bill_amount": "Unable to calculate payment for this offer."}
            )

        attrs["payment"] = payment
        return attrs


class OfferRedeemSerializer(OfferScanSerializer):
    def validate(self, attrs):
        attrs = super().validate(attrs)
        user = self.context["request"].user
        offer = self.context["offer"]
        branch = attrs["branch"]

        can_redeem, message = can_user_redeem_offer(user, offer, branch)
        if not can_redeem:
            raise serializers.ValidationError({"non_field_errors": [message]})
        return attrs
