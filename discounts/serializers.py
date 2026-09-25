from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .location_utils import branch_distance_km
from .models import (
    Address,
    Branch,
    Business,
    Category,
    DeviceToken,
    Notification,
    Offer,
    OfferRedemption,
    PasswordResetToken,
    UserPreferences,
)
from .offer_pricing import compute_offer_payment
from .offer_utils import (
    UserOfferUsageStatus,
    build_media_url,
    build_offer_image_urls,
    branch_highlight_queryset,
    build_user_redemption_map,
    get_highest_discount_active_offer,
    get_user_offer_usage_status,
)

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    name = serializers.CharField(
        write_only=True,
        max_length=301,
        trim_whitespace=True,
        error_messages={
            "required": "Full name is required.",
            "blank": "Full name cannot be empty.",
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

    class Meta:
        model = User
        fields = ("name", "email", "password", "password_confirm")

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Full name is required.")
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
        first_name, _, last_name = name.partition(" ")
        return User.objects.create_user(
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            **validated_data,
        )


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name"]


class OfferSerializer(serializers.ModelSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    category_id = serializers.IntegerField(source="business.category.id", read_only=True)
    category_name = serializers.CharField(
        source="business.category.name", read_only=True
    )
    category = CategorySerializer(source="business.category", read_only=True)
    branch_ids = serializers.PrimaryKeyRelatedField(
        many=True, source="branches", read_only=True
    )
    is_active = serializers.SerializerMethodField()
    image_urls = serializers.SerializerMethodField()
    user_redemption_count = serializers.SerializerMethodField()
    user_remaining_uses = serializers.SerializerMethodField()
    is_available_for_user = serializers.SerializerMethodField()
    last_redeemed_at = serializers.SerializerMethodField()
    period_resets_at = serializers.SerializerMethodField()
    nearest_distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "business_id",
            "business_name",
            "category_id",
            "category_name",
            "category",
            "branch_ids",
            "offer_type",
            "redemption_mode",
            "is_online",
            "title",
            "description",
            "detailed_description",
            "external_url",
            "external_url_label",
            "image_urls",
            "discount_percent",
            "item_name",
            "included_items",
            "original_price",
            "discounted_price",
            "usage_limit_type",
            "usage_limit_count",
            "is_enabled",
            "is_time_limited",
            "starts_at",
            "ends_at",
            "qr_code",
            "is_active",
            "user_redemption_count",
            "user_remaining_uses",
            "is_available_for_user",
            "last_redeemed_at",
            "period_resets_at",
            "nearest_distance_km",
        ]

    def _get_usage_status(self, obj: Offer) -> UserOfferUsageStatus | None:
        usage_map = self.context.get("user_offer_usage_by_id")
        if usage_map is not None:
            return usage_map.get(obj.id)

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if (
            user
            and user.is_authenticated
            and user.account_type == user.AccountType.CONSUMER
        ):
            return get_user_offer_usage_status(user, obj)
        return None

    def get_user_redemption_count(self, obj: Offer) -> int | None:
        usage = self._get_usage_status(obj)
        return usage.redemption_count if usage else None

    def get_user_remaining_uses(self, obj: Offer) -> int | None:
        usage = self._get_usage_status(obj)
        return usage.remaining_uses if usage else None

    def get_is_available_for_user(self, obj: Offer) -> bool | None:
        usage = self._get_usage_status(obj)
        return usage.is_available_for_user if usage else None

    def get_last_redeemed_at(self, obj: Offer):
        usage = self._get_usage_status(obj)
        return usage.last_redeemed_at if usage else None

    def get_period_resets_at(self, obj: Offer):
        usage = self._get_usage_status(obj)
        return usage.period_resets_at if usage else None

    def get_nearest_distance_km(self, obj: Offer):
        location = self.context.get("user_location")
        if location is None:
            return None
        branches = list(obj.branches.all())
        if not branches:
            return None
        distance = min(branch_distance_km(branch, location) for branch in branches)
        return round(distance, 2)

    def get_is_active(self, obj: Offer) -> bool:
        return obj.is_active

    def get_image_urls(self, obj: Offer) -> list[str]:
        return build_offer_image_urls(obj, self.context.get("request"))


class DiscountFeaturedBranchSerializer(serializers.ModelSerializer):
    formattedAddress = serializers.CharField(source="formatted_address", read_only=True)

    class Meta:
        model = Branch
        fields = ["id", "name", "formattedAddress", "latitude", "longitude"]


class DiscountOfferSerializer(OfferSerializer):
    featured_branch = serializers.SerializerMethodField()
    business_logo_url = serializers.SerializerMethodField()

    class Meta(OfferSerializer.Meta):
        fields = OfferSerializer.Meta.fields + [
            "featured_branch",
            "business_logo_url",
        ]

    def get_featured_branch(self, obj: Offer):
        branches = list(obj.branches.all())
        if not branches:
            return None
        branch = sorted(branches, key=lambda item: (item.name, item.id))[0]
        return DiscountFeaturedBranchSerializer(branch).data

    def get_business_logo_url(self, obj: Offer) -> str | None:
        return build_media_url(self.context.get("request"), obj.business.logo)


class AvailedOfferBranchSerializer(serializers.ModelSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    formattedAddress = serializers.CharField(source="formatted_address", read_only=True)

    class Meta:
        model = Branch
        fields = [
            "id",
            "name",
            "business_id",
            "business_name",
            "formattedAddress",
            "latitude",
            "longitude",
        ]


class AvailedOfferSummarySerializer(serializers.ModelSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    category_id = serializers.IntegerField(source="business.category.id", read_only=True)
    category_name = serializers.CharField(
        source="business.category.name", read_only=True
    )
    image_urls = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "business_id",
            "business_name",
            "category_id",
            "category_name",
            "offer_type",
            "redemption_mode",
            "is_online",
            "title",
            "description",
            "detailed_description",
            "external_url",
            "external_url_label",
            "image_urls",
            "discount_percent",
            "item_name",
            "included_items",
            "original_price",
            "discounted_price",
        ]

    def get_image_urls(self, obj: Offer) -> list[str]:
        return build_offer_image_urls(obj, self.context.get("request"))


class UserAvailedOfferSerializer(serializers.ModelSerializer):
    offer = AvailedOfferSummarySerializer(read_only=True)
    branch = AvailedOfferBranchSerializer(read_only=True)

    class Meta:
        model = OfferRedemption
        fields = ["id", "redeemed_at", "offer", "branch"]


class OfferUsageSerializer(serializers.Serializer):
    offer_id = serializers.IntegerField()
    user_redemption_count = serializers.IntegerField()
    user_remaining_uses = serializers.IntegerField()
    max_uses = serializers.IntegerField()
    is_available_for_user = serializers.BooleanField()
    last_redeemed_at = serializers.DateTimeField(allow_null=True)
    period_resets_at = serializers.DateTimeField(allow_null=True)
    message = serializers.CharField(allow_blank=True)

    @classmethod
    def from_usage(cls, offer_id: int, usage: UserOfferUsageStatus):
        return cls(
            {
                "offer_id": offer_id,
                **usage.as_dict(),
            }
        )


class OfferPaymentPreviewSerializer(serializers.Serializer):
    offer_type = serializers.CharField()
    item_name = serializers.CharField(allow_null=True, allow_blank=True)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    original_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    amount_to_pay = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    bill_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    requires_bill_amount = serializers.BooleanField()
    summary = serializers.CharField(allow_null=True)

    @classmethod
    def from_preview(cls, preview):
        return cls(preview.as_dict())


class OfferPaymentPreviewRequestSerializer(serializers.Serializer):
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
        bill_amount = attrs.get("bill_amount")

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
        attrs["payment"] = payment
        return attrs


class OfferQRBranchSerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source="business.name", read_only=True)
    formatted_address = serializers.CharField(read_only=True)

    class Meta:
        model = Branch
        fields = ["id", "name", "business_name", "formatted_address"]


class OfferQRSummarySerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source="business.name", read_only=True)
    image_urls = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "business_name",
            "title",
            "description",
            "detailed_description",
            "external_url",
            "external_url_label",
            "offer_type",
            "redemption_mode",
            "is_online",
            "discount_percent",
            "item_name",
            "included_items",
            "original_price",
            "discounted_price",
            "image_urls",
            "is_active",
        ]

    def get_image_urls(self, obj: Offer) -> list[str]:
        return build_offer_image_urls(obj, self.context.get("request"))


class BranchTopOfferSerializer(serializers.ModelSerializer):
    image_urls = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "title",
            "description",
            "detailed_description",
            "external_url",
            "external_url_label",
            "offer_type",
            "redemption_mode",
            "is_online",
            "discount_percent",
            "item_name",
            "included_items",
            "original_price",
            "discounted_price",
            "image_urls",
            "is_active",
        ]

    def get_image_urls(self, obj: Offer) -> list[str]:
        return build_offer_image_urls(obj, self.context.get("request"))

    def get_is_active(self, obj: Offer) -> bool:
        return obj.is_active


class BranchHighlightSerializer(serializers.ModelSerializer):
    business_logo_url = serializers.SerializerMethodField()
    highest_discount_percent = serializers.SerializerMethodField()
    highest_discount_offer = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = [
            "business_logo_url",
            "highest_discount_percent",
            "highest_discount_offer",
        ]

    def get_business_logo_url(self, obj: Branch) -> str | None:
        business = getattr(obj, "business", None)
        if business is None:
            return None
        return build_media_url(self.context.get("request"), business.logo)

    def get_highest_discount_percent(self, obj: Branch):
        annotated = getattr(obj, "highest_discount_percent", None)
        if annotated is not None:
            return annotated
        top_offer = get_highest_discount_active_offer(obj)
        return top_offer.discount_percent if top_offer else None

    def get_highest_discount_offer(self, obj: Branch):
        top_offer = get_highest_discount_active_offer(obj)
        if top_offer is None:
            return None
        return BranchTopOfferSerializer(top_offer, context=self.context).data


class MapBranchSerializer(BranchHighlightSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    category_id = serializers.IntegerField(source="business.category.id", read_only=True)
    category_name = serializers.CharField(source="business.category.name", read_only=True)
    category = CategorySerializer(source="business.category", read_only=True)
    formattedAddress = serializers.CharField(source="formatted_address", read_only=True)
    distance_km = serializers.SerializerMethodField()

    class Meta(BranchHighlightSerializer.Meta):
        fields = BranchHighlightSerializer.Meta.fields + [
            "id",
            "business_id",
            "business_name",
            "category_id",
            "category_name",
            "category",
            "name",
            "latitude",
            "longitude",
            "formattedAddress",
            "distance_km",
        ]

    def get_distance_km(self, obj: Branch):
        location = self.context.get("user_location")
        if location is None:
            return None
        distance = branch_distance_km(obj, location)
        return round(distance, 2)


class MapBusinessSerializer(serializers.ModelSerializer):
    """Legacy alias kept for backward compatibility; map data is branch-based."""

    highest_discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, read_only=True
    )
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Business
        fields = [
            "id",
            "name",
            "category_name",
            "highest_discount_percent",
        ]


class LoginUserSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "email", "phone", "name")

    def get_id(self, obj: User) -> str:
        return str(obj.pk)

    def get_name(self, obj: User) -> str:
        return obj.get_full_name().strip()


class PhoneAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField(
        trim_whitespace=True,
        error_messages={
            "required": "Firebase ID token is required.",
            "blank": "Firebase ID token cannot be empty.",
        },
    )

    def validate_id_token(self, value: str) -> str:
        token = value.strip()
        if not token:
            raise serializers.ValidationError("Firebase ID token cannot be empty.")
        return token


class ConsumerProfileSerializer(serializers.Serializer):
    """Consumer self-service profile. Phone/email are read-only; name is editable."""

    id = serializers.SerializerMethodField(read_only=True)
    email = serializers.EmailField(read_only=True)
    phone = serializers.CharField(read_only=True, allow_null=True, required=False)
    name = serializers.CharField(
        max_length=301,
        trim_whitespace=True,
        error_messages={
            "required": "Full name is required.",
            "blank": "Full name cannot be empty.",
        },
    )

    def get_id(self, obj: User) -> str:
        return str(obj.pk)

    def to_representation(self, instance: User) -> dict:
        return {
            "id": str(instance.pk),
            "email": instance.email,
            "phone": instance.phone,
            "name": instance.get_full_name().strip(),
        }

    def validate_name(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Full name is required.")
        return cleaned

    def update(self, instance: User, validated_data: dict) -> User:
        if "name" not in validated_data:
            return instance
        name = validated_data["name"]
        first_name, _, last_name = name.partition(" ")
        instance.first_name = first_name
        instance.last_name = last_name
        instance.save(update_fields=["first_name", "last_name"])
        return instance


class AddressSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    street = serializers.CharField(
        error_messages={
            "required": "Street is required.",
            "blank": "Street cannot be empty.",
        }
    )
    houseNumber = serializers.CharField(
        source="house_number",
        error_messages={
            "required": "House number is required.",
            "blank": "House number cannot be empty.",
        },
    )
    postalCode = serializers.CharField(
        source="postal_code",
        error_messages={
            "required": "Postal code is required.",
            "blank": "Postal code cannot be empty.",
        },
    )
    city = serializers.CharField(
        error_messages={
            "required": "City is required.",
            "blank": "City cannot be empty.",
        }
    )
    county = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        error_messages={
            "blank": "County cannot be empty.",
        },
    )
    isDefault = serializers.BooleanField(
        source="is_default", required=False, default=False
    )
    deliveryInstructions = serializers.CharField(
        source="delivery_instructions",
        required=False,
        allow_blank=True,
        default="",
    )
    formattedAddress = serializers.CharField(source="formatted_address", read_only=True)
    latitude = serializers.FloatField(
        error_messages={
            "required": "Latitude is required.",
            "invalid": "Latitude must be a valid number.",
        }
    )
    longitude = serializers.FloatField(
        error_messages={
            "required": "Longitude is required.",
            "invalid": "Longitude must be a valid number.",
        }
    )

    class Meta:
        model = Address
        fields = [
            "id",
            "street",
            "houseNumber",
            "postalCode",
            "city",
            "county",
            "latitude",
            "longitude",
            "isDefault",
            "deliveryInstructions",
            "formattedAddress",
        ]

    def validate_latitude(self, value: float) -> float:
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_longitude(self, value: float) -> float:
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value

    def get_id(self, obj: Address) -> str:
        return f"addr_{obj.pk}"

    def _apply_default_logic(self, user, is_default: bool, exclude_pk: int | None = None):
        if is_default:
            queryset = user.addresses.filter(is_default=True)
            if exclude_pk is not None:
                queryset = queryset.exclude(pk=exclude_pk)
            queryset.update(is_default=False)
            return True

        queryset = user.addresses
        if exclude_pk is not None:
            queryset = queryset.exclude(pk=exclude_pk)
        if not queryset.filter(is_default=True).exists():
            next_address = queryset.order_by("id").first()
            if next_address:
                next_address.is_default = True
                next_address.save(update_fields=["is_default"])
                return False
        return is_default

    def create(self, validated_data):
        user = self.context["request"].user
        is_default = validated_data.get("is_default", False)
        if is_default:
            user.addresses.filter(is_default=True).update(is_default=False)
        elif not user.addresses.exists():
            validated_data["is_default"] = True
        return Address.objects.create(user=user, **validated_data)

    def update(self, instance, validated_data):
        user = self.context["request"].user
        is_default = validated_data.get("is_default", instance.is_default)

        if "is_default" in validated_data and not is_default and instance.is_default:
            other_addresses = user.addresses.exclude(pk=instance.pk)
            if not other_addresses.exists():
                validated_data["is_default"] = True
            else:
                self._apply_default_logic(user, is_default=False, exclude_pk=instance.pk)
        elif is_default and not instance.is_default:
            self._apply_default_logic(user, is_default=True, exclude_pk=instance.pk)

        return super().update(instance, validated_data)


class LoginTokenObtainPairSerializer(TokenObtainPairSerializer):
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
        user = self.user
        if user.is_business_account:
            raise serializers.ValidationError(
                {"email": "Please use the business login endpoint for business accounts."}
            )
        data.pop("refresh", None)
        data["user"] = LoginUserSerializer(user).data
        data["addresses"] = AddressSerializer(
            user.addresses.order_by("-is_default", "id"), many=True
        ).data
        return data


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(
        error_messages={
            "required": "Email is required.",
            "blank": "Email cannot be empty.",
            "invalid": "Enter a valid email address.",
        }
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.UUIDField(
        error_messages={
            "required": "Reset token is required.",
            "invalid": "Enter a valid reset token.",
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

    def validate_token(self, value):
        try:
            reset_token = PasswordResetToken.objects.select_related("user").get(
                token=value
            )
        except PasswordResetToken.DoesNotExist as exc:
            raise serializers.ValidationError(
                "Invalid or expired reset token."
            ) from exc
        if not reset_token.is_valid:
            raise serializers.ValidationError("Invalid or expired reset token.")
        self.context["reset_token"] = reset_token
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        user = self.context["reset_token"].user
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs

    def save(self, **kwargs):
        reset_token = self.context["reset_token"]
        user = reset_token.user
        user.set_password(self.validated_data["password"])
        user.save(update_fields=["password"])
        reset_token.mark_used()
        return user


class UserPreferencesSerializer(serializers.ModelSerializer):
    preferred_categories = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Category.objects.all(), required=False
    )
    preferred_category_details = CategorySerializer(
        source="preferred_categories", many=True, read_only=True
    )

    class Meta:
        model = UserPreferences
        fields = [
            "notifications_enabled",
            "theme_preference",
            "preferred_categories",
            "preferred_category_details",
        ]


class DeviceTokenSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
    platform = serializers.ChoiceField(choices=DeviceToken.Platform.choices)

    def create(self, validated_data):
        user = self.context["request"].user
        token = validated_data["token"]
        platform = validated_data["platform"]
        device, _ = DeviceToken.objects.update_or_create(
            token=token,
            defaults={"user": user, "platform": platform},
        )
        return device


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "title",
            "body",
            "data",
            "is_read",
            "read_at",
            "created_at",
        ]
        read_only_fields = fields
