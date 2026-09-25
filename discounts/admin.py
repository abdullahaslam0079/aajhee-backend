from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q

from .models import (
    Address,
    Branch,
    BranchContact,
    BranchFulfillmentSettings,
    Business,
    BusinessCategory,
    Category,
    City,
    Country,
    DealSource,
    DeviceToken,
    Notification,
    Offer,
    OfferBranchStats,
    OfferEngagementStats,
    OfferGalleryImage,
    OfferLike,
    OfferRedemption,
    OfferScan,
    OfferViewEvent,
    Order,
    Product,
    User,
    UserPreferences,
)


class UserCreationForm(BaseUserCreationForm):
    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ("email",)


class UserChangeForm(BaseUserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm

    ordering = ("email",)
    list_display = (
        "email",
        "phone",
        "account_type",
        "is_staff",
        "is_active",
        "is_superuser",
    )
    search_fields = ("email", "phone", "firebase_uid")
    list_filter = ("account_type", "is_staff", "is_superuser", "is_active")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Phone auth", {"fields": ("phone", "firebase_uid")}),
        ("Account", {"fields": ("account_type",)}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "account_type"),
            },
        ),
    )

    filter_horizontal = ("groups", "user_permissions")


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


class DealSourceInline(admin.TabularInline):
    model = DealSource
    extra = 0
    fields = (
        "name",
        "kind",
        "listing_url",
        "feed_url",
        "is_enabled",
        "is_online",
        "max_items",
        "last_synced_at",
    )
    readonly_fields = ("last_synced_at",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "parent", "sort_order", "is_active")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)


class BusinessCategoryInline(admin.TabularInline):
    model = BusinessCategory
    extra = 1
    autocomplete_fields = ("category",)


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "category", "presence_mode", "online_coverage")
    list_filter = ("category", "presence_mode", "online_coverage")
    search_fields = ("name", "owner__email")
    inlines = [BusinessCategoryInline, BranchInline, DealSourceInline]


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "business", "city", "latitude", "longitude")
    list_filter = ("city", "business__category")
    search_fields = ("name", "business__name", "city")


class OfferGalleryImageInline(admin.TabularInline):
    model = OfferGalleryImage
    extra = 1
    fields = ("image", "source_url", "sort_order")
    ordering = ("sort_order", "id")


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "business",
        "title",
        "offer_type",
        "redemption_mode",
        "is_online",
        "discount_percent",
        "usage_limit_type",
        "is_enabled",
        "origin",
        "review_status",
        "is_time_limited",
        "has_image",
        "view_count",
        "like_count",
        "unique_viewers",
    )
    list_filter = (
        "offer_type",
        "redemption_mode",
        "is_online",
        "is_enabled",
        "origin",
        "review_status",
        "is_time_limited",
        "business__category",
    )
    search_fields = ("title", "business__name", "item_name", "source_url", "source_key")
    filter_horizontal = ("branches",)
    exclude = ("image",)
    inlines = [OfferGalleryImageInline]
    list_select_related = ("business", "engagement_stats")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("engagement_stats").annotate(
            annotated_unique_viewers=Count(
                "view_events__user",
                distinct=True,
                filter=Q(view_events__user__isnull=False),
            )
        )

    @admin.display(boolean=True, description="Images")
    def has_image(self, obj):
        return obj.gallery_images.exists() or bool(obj.image)

    @admin.display(description="Views", ordering="engagement_stats__view_count")
    def view_count(self, obj):
        try:
            return obj.engagement_stats.view_count
        except ObjectDoesNotExist:
            return 0

    @admin.display(description="Likes", ordering="engagement_stats__like_count")
    def like_count(self, obj):
        try:
            return obj.engagement_stats.like_count
        except ObjectDoesNotExist:
            return 0

    @admin.display(description="Unique viewers", ordering="annotated_unique_viewers")
    def unique_viewers(self, obj):
        return int(getattr(obj, "annotated_unique_viewers", 0) or 0)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            # M2M branches are not available until save_related; notify there.
            obj._notify_on_create = True

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        offer = form.instance
        first = offer.gallery_images.order_by("sort_order", "id").first()
        offer.image = first.image.name if first else None
        offer.save(update_fields=["image"])
        if getattr(offer, "_notify_on_create", False):
            from .notification_utils import notify_favorited_business_new_offer

            notify_favorited_business_new_offer(offer)
            offer._notify_on_create = False


@admin.register(DealSource)
class DealSourceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "business",
        "kind",
        "is_enabled",
        "max_items",
        "last_synced_at",
    )
    list_filter = ("kind", "is_enabled", "is_online")
    search_fields = ("name", "business__name", "listing_url", "feed_url")
    readonly_fields = ("last_synced_at", "last_error", "created_at")


@admin.register(OfferEngagementStats)
class OfferEngagementStatsAdmin(admin.ModelAdmin):
    list_display = ("offer", "view_count", "like_count")
    search_fields = ("offer__title", "offer__business__name")
    readonly_fields = ("offer", "view_count", "like_count")


@admin.register(OfferViewEvent)
class OfferViewEventAdmin(admin.ModelAdmin):
    list_display = ("offer", "user", "viewed_on")
    list_filter = ("viewed_on",)
    search_fields = ("offer__title", "user__email")
    readonly_fields = ("offer", "user", "viewed_on")


@admin.register(OfferBranchStats)
class OfferBranchStatsAdmin(admin.ModelAdmin):
    list_display = ("offer", "branch", "scan_count", "avail_count")


@admin.register(OfferScan)
class OfferScanAdmin(admin.ModelAdmin):
    list_display = ("offer", "branch", "user", "scanned_at")


@admin.register(OfferRedemption)
class OfferRedemptionAdmin(admin.ModelAdmin):
    list_display = ("offer", "branch", "user", "redeemed_at")


@admin.register(UserPreferences)
class UserPreferencesAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "notifications_enabled", "theme_preference")
    list_filter = ("theme_preference", "notifications_enabled")


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "platform", "updated_at")
    list_filter = ("platform",)
    search_fields = ("user__email", "token")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "type", "title", "read_at", "created_at")
    list_filter = ("type",)
    search_fields = ("user__email", "title", "body")
    readonly_fields = ("created_at",)


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "street",
        "house_number",
        "city",
        "is_default",
    )
    list_filter = ("is_default", "city")
    search_fields = ("user__email", "street", "city")


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name")
    search_fields = ("code", "name")


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "country", "name_normalized")
    list_filter = ("country",)
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "business",
        "category",
        "base_price",
        "sale_price",
        "is_enabled",
    )
    list_filter = ("is_enabled", "category")
    search_fields = ("name", "business__name")
    filter_horizontal = ("branches",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "business",
        "branch",
        "status",
        "fulfillment_type",
        "payment_method",
        "total",
        "placed_at",
    )
    list_filter = ("status", "fulfillment_type", "payment_method")
    search_fields = ("public_id", "business__name", "user__email")
    readonly_fields = ("public_id", "placed_at")


@admin.register(BranchContact)
class BranchContactAdmin(admin.ModelAdmin):
    list_display = ("branch", "contact_type", "value", "is_primary")
    list_filter = ("contact_type",)


@admin.register(BranchFulfillmentSettings)
class BranchFulfillmentSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "branch",
        "pickup_enabled",
        "local_same_day_enabled",
        "nationwide_enabled",
        "bank_transfer_enabled",
    )
