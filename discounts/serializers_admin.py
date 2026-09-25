from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Branch, Business, Category, DealSource, Offer, OfferViewEvent
from .serializers_business import (
    BranchSerializer,
    BusinessOfferSerializer,
    BusinessProfileSerializer,
    BusinessRegisterSerializer,
)

User = get_user_model()


class AdminProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
            "date_joined",
        ]
        read_only_fields = fields


class AdminLoginTokenObtainPairSerializer(TokenObtainPairSerializer):
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
        if not self.user.is_staff:
            raise serializers.ValidationError(
                {"email": "This account does not have admin access."}
            )
        data.pop("refresh", None)
        data["admin"] = AdminProfileSerializer(self.user).data
        return data


class AdminCategorySerializer(serializers.ModelSerializer):
    business_count = serializers.SerializerMethodField()
    parent_id = serializers.IntegerField(source="parent.id", read_only=True, allow_null=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "parent_id", "sort_order", "is_active", "business_count"]
        read_only_fields = ["id", "business_count", "parent_id"]

    def get_business_count(self, obj: Category) -> int:
        return getattr(
            obj,
            "business_count",
            obj.businesses.count() + obj.primary_businesses.count(),
        )

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Category name is required.")
        return name


class AdminUserSerializer(serializers.ModelSerializer):
    business_id = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "account_type",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
            "business_id",
            "business_name",
        ]
        read_only_fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "account_type",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
            "business_id",
            "business_name",
        ]

    def get_business_id(self, obj: User) -> int | None:
        profile = getattr(obj, "business_profile", None)
        return profile.id if profile else None

    def get_business_name(self, obj: User) -> str | None:
        profile = getattr(obj, "business_profile", None)
        return profile.name if profile else None


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["is_active", "first_name", "last_name"]


class AdminBusinessSerializer(BusinessProfileSerializer):
    owner_id = serializers.IntegerField(source="owner.id", read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    owner_is_active = serializers.BooleanField(source="owner.is_active", read_only=True)
    branch_count = serializers.SerializerMethodField()
    offer_count = serializers.SerializerMethodField()
    scan_count = serializers.SerializerMethodField()
    redemption_count = serializers.SerializerMethodField()
    view_count = serializers.SerializerMethodField()
    like_count = serializers.SerializerMethodField()

    class Meta(BusinessProfileSerializer.Meta):
        fields = BusinessProfileSerializer.Meta.fields + [
            "owner_id",
            "owner_email",
            "owner_is_active",
            "branch_count",
            "offer_count",
            "scan_count",
            "redemption_count",
            "view_count",
            "like_count",
        ]
        read_only_fields = BusinessProfileSerializer.Meta.read_only_fields + [
            "owner_id",
            "owner_email",
            "owner_is_active",
            "branch_count",
            "offer_count",
            "scan_count",
            "redemption_count",
            "view_count",
            "like_count",
        ]

    def get_branch_count(self, obj: Business) -> int:
        return getattr(obj, "annotated_branch_count", obj.branches.count())

    def get_offer_count(self, obj: Business) -> int:
        return getattr(obj, "annotated_offer_count", obj.offers.count())

    def get_scan_count(self, obj: Business) -> int:
        return int(getattr(obj, "annotated_scan_count", 0) or 0)

    def get_redemption_count(self, obj: Business) -> int:
        return int(getattr(obj, "annotated_redemption_count", 0) or 0)

    def _engagement_stats(self, obj: Business):
        try:
            return obj.engagement_stats
        except ObjectDoesNotExist:
            return None

    @extend_schema_field(OpenApiTypes.INT)
    def get_view_count(self, obj: Business) -> int:
        stats = self._engagement_stats(obj)
        return stats.view_count if stats else 0

    @extend_schema_field(OpenApiTypes.INT)
    def get_like_count(self, obj: Business) -> int:
        stats = self._engagement_stats(obj)
        return stats.like_count if stats else 0


class AdminBusinessCreateSerializer(BusinessRegisterSerializer):
    """Creates owner + business from admin panel (same shape as merchant register)."""


class AdminBranchSerializer(BranchSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)

    class Meta(BranchSerializer.Meta):
        fields = BranchSerializer.Meta.fields + ["business_id", "business_name"]


class AdminOfferSerializer(BusinessOfferSerializer):
    business_id = serializers.PrimaryKeyRelatedField(
        queryset=Business.objects.all(),
        source="business",
        required=True,
        error_messages={
            "required": "Business is required.",
            "does_not_exist": "Selected business does not exist.",
        },
    )
    business_name = serializers.CharField(source="business.name", read_only=True)
    unique_viewers = serializers.SerializerMethodField(
        help_text="Distinct authenticated users who opened this offer at least once.",
    )
    source_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta(BusinessOfferSerializer.Meta):
        fields = BusinessOfferSerializer.Meta.fields + [
            "business_id",
            "business_name",
            "unique_viewers",
            "origin",
            "source_id",
            "source_url",
            "source_key",
            "review_status",
            "last_seen_at",
            "last_synced_at",
            "unavailable_reason",
            "disabled_by",
        ]
        read_only_fields = list(
            getattr(BusinessOfferSerializer.Meta, "read_only_fields", [])
        ) + [
            "business_name",
            "view_count",
            "like_count",
            "unique_viewers",
            "origin",
            "source_id",
            "source_url",
            "source_key",
            "review_status",
            "last_seen_at",
            "last_synced_at",
            "unavailable_reason",
            "disabled_by",
        ]

    @extend_schema_field(OpenApiTypes.INT)
    def get_unique_viewers(self, obj: Offer) -> int:
        annotated = getattr(obj, "annotated_unique_viewers", None)
        if annotated is not None:
            return int(annotated)
        return (
            OfferViewEvent.objects.filter(offer=obj, user__isnull=False)
            .values("user_id")
            .distinct()
            .count()
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if (
            business is None
            and self.instance is not None
            and isinstance(self.instance, Offer)
        ):
            business = self.instance.business
            self.context["business"] = business
        if business is not None:
            queryset = business.branches.all()
            branch_field = self.fields["branch_ids"]
            branch_field.queryset = queryset
            branch_field.child_relation.queryset = queryset
        else:
            # Allow any branch on create until business is known; validated in validate().
            branch_field = self.fields["branch_ids"]
            branch_field.queryset = Branch.objects.all()
            branch_field.child_relation.queryset = Branch.objects.all()

    def _get_business(self):
        if "business" in self.context and self.context["business"] is not None:
            return self.context["business"]
        if self.instance is not None and isinstance(self.instance, Offer):
            return self.instance.business
        raise serializers.ValidationError({"business_id": "Business is required."})

    def validate(self, attrs):
        business = attrs.get("business")
        if (
            business is None
            and self.instance is not None
            and isinstance(self.instance, Offer)
        ):
            business = self.instance.business
        if business is None:
            raise serializers.ValidationError({"business_id": "Business is required."})
        self.context["business"] = business

        branches = attrs.get("branch_ids")
        if branches is not None:
            invalid = [
                branch.pk for branch in branches if branch.business_id != business.pk
            ]
            if invalid:
                raise serializers.ValidationError(
                    {
                        "branch_ids": (
                            "One or more branches do not belong to the selected business."
                        )
                    }
                )

        return super().validate(attrs)

    def create(self, validated_data):
        business = validated_data.pop("business")
        self.context["business"] = business
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("business", None)
        self.context["business"] = instance.business
        if "is_enabled" in validated_data:
            if validated_data["is_enabled"]:
                validated_data["disabled_by"] = ""
                validated_data["unavailable_reason"] = ""
                if instance.review_status == Offer.ReviewStatus.PENDING:
                    validated_data["review_status"] = Offer.ReviewStatus.APPROVED
            elif instance.is_enabled:
                validated_data["disabled_by"] = Offer.DisabledBy.ADMIN
        return super().update(instance, validated_data)


class AdminDealSourceSerializer(serializers.ModelSerializer):
    business_id = serializers.IntegerField(read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)

    class Meta:
        model = DealSource
        fields = [
            "id",
            "business_id",
            "business_name",
            "name",
            "kind",
            "listing_url",
            "feed_url",
            "is_enabled",
            "is_online",
            "max_items",
            "last_synced_at",
            "last_error",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "business_id",
            "business_name",
            "last_synced_at",
            "last_error",
            "created_at",
        ]

    def validate_max_items(self, value: int) -> int:
        if value < 1:
            raise serializers.ValidationError("max_items must be at least 1.")
        if value > 500:
            raise serializers.ValidationError("max_items cannot exceed 500.")
        return value

    def validate(self, attrs):
        kind = attrs.get("kind", getattr(self.instance, "kind", DealSource.Kind.BRAND_LISTING))
        listing_url = attrs.get(
            "listing_url", getattr(self.instance, "listing_url", "") if self.instance else ""
        )
        feed_url = attrs.get(
            "feed_url", getattr(self.instance, "feed_url", "") if self.instance else ""
        )
        if kind == DealSource.Kind.BRAND_LISTING and not (listing_url or "").strip():
            raise serializers.ValidationError(
                {"listing_url": "Listing URL is required for brand listing sources."}
            )
        if kind == DealSource.Kind.AFFILIATE_FEED and not (feed_url or "").strip():
            raise serializers.ValidationError(
                {"feed_url": "Feed URL is required for affiliate sources."}
            )
        name = attrs.get("name")
        if name is None:
            name = getattr(self.instance, "name", "") if self.instance else ""
        if not (name or "").strip():
            from .offer_sync import default_source_name

            url = listing_url or feed_url
            attrs["name"] = default_source_name(url) or "Deal source"
        return attrs

    def create(self, validated_data):
        business = self.context["business"]
        return DealSource.objects.create(business=business, **validated_data)
