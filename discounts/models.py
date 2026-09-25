import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    class AccountType(models.TextChoices):
        CONSUMER = "consumer", "Consumer"
        BUSINESS = "business", "Business"

    username = None
    email = models.EmailField(unique=True)
    phone = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        help_text="E.164 phone number from Firebase Phone Auth.",
    )
    firebase_uid = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        help_text="Firebase Auth UID linked to this account.",
    )
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.CONSUMER,
    )
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    @property
    def is_business_account(self) -> bool:
        return self.account_type == self.AccountType.BUSINESS

    def __str__(self) -> str:
        return self.phone or self.email


class Country(models.Model):
    code = models.CharField(max_length=2, unique=True)
    name = models.CharField(max_length=80)

    class Meta:
        verbose_name_plural = "countries"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class City(models.Model):
    country = models.ForeignKey(
        Country, on_delete=models.CASCADE, related_name="cities"
    )
    name = models.CharField(max_length=80)
    name_normalized = models.CharField(max_length=80, db_index=True)

    class Meta:
        verbose_name_plural = "cities"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["country", "name_normalized"],
                name="unique_city_per_country",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.country.code}"


class Category(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=100, blank=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="unique_category_name_per_parent",
            )
        ]

    def __str__(self) -> str:
        if self.parent_id:
            return f"{self.parent} → {self.name}"
        return self.name


class Business(models.Model):
    class PresenceMode(models.TextChoices):
        ONLINE_ONLY = "online_only", "Online only"
        INSTORE_ONLY = "instore_only", "In-store only"
        HYBRID = "hybrid", "Online and in-store"

    class OnlineCoverage(models.TextChoices):
        CITY = "city", "City"
        COUNTRY = "country", "Whole country"

    owner = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="business_profile"
    )
    name = models.CharField(max_length=120)
    logo = models.ImageField(upload_to="business_logos/", null=True, blank=True)
    # Legacy primary category — kept for backward-compatible APIs.
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_businesses",
    )
    categories = models.ManyToManyField(
        Category,
        through="BusinessCategory",
        related_name="businesses",
        blank=True,
    )
    presence_mode = models.CharField(
        max_length=20,
        choices=PresenceMode.choices,
        default=PresenceMode.HYBRID,
    )
    online_coverage = models.CharField(
        max_length=20,
        choices=OnlineCoverage.choices,
        default=OnlineCoverage.CITY,
    )
    primary_country = models.ForeignKey(
        Country,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_businesses",
    )
    primary_city = models.ForeignKey(
        City,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_businesses",
    )

    def __str__(self) -> str:
        return self.name


class BusinessCategory(models.Model):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="business_categories"
    )
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="business_links"
    )

    class Meta:
        verbose_name_plural = "business categories"
        constraints = [
            models.UniqueConstraint(
                fields=["business", "category"],
                name="unique_business_category",
            )
        ]

    def __str__(self) -> str:
        return f"{self.business_id}:{self.category_id}"


class Branch(models.Model):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="branches"
    )
    name = models.CharField(max_length=120)
    street = models.CharField(max_length=120)
    house_number = models.CharField(max_length=20)
    postal_code = models.CharField(max_length=20)
    city = models.CharField(max_length=80)
    city_ref = models.ForeignKey(
        City,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="branches",
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)

    class Meta:
        verbose_name_plural = "branches"
        ordering = ["name", "id"]

    @property
    def formatted_address(self) -> str:
        return f"{self.street} {self.house_number}, {self.postal_code} {self.city}"

    def __str__(self) -> str:
        return f"{self.business.name} - {self.name}"


class BranchContact(models.Model):
    class ContactType(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"

    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="contacts"
    )
    contact_type = models.CharField(max_length=20, choices=ContactType.choices)
    value = models.CharField(max_length=160)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "contact_type", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "contact_type", "value"],
                name="unique_branch_contact_value",
            )
        ]

    def __str__(self) -> str:
        return f"{self.branch_id}:{self.contact_type}"


class BranchFulfillmentSettings(models.Model):
    class CustomerCancelPolicy(models.TextChoices):
        DISABLED = "disabled", "Customer cannot cancel"
        WINDOW_MINUTES = "window_minutes", "Cancel within window"

    branch = models.OneToOneField(
        Branch, on_delete=models.CASCADE, related_name="fulfillment_settings"
    )
    pickup_enabled = models.BooleanField(default=True)
    pickup_radius_km = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("15.00")
    )
    local_same_day_enabled = models.BooleanField(default=True)
    local_delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    local_max_delivery_hours = models.PositiveIntegerField(default=24)
    nationwide_enabled = models.BooleanField(default=False)
    nationwide_delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    nationwide_max_delivery_hours = models.PositiveIntegerField(default=72)
    customer_cancel_policy = models.CharField(
        max_length=32,
        choices=CustomerCancelPolicy.choices,
        default=CustomerCancelPolicy.WINDOW_MINUTES,
    )
    customer_cancel_window_minutes = models.PositiveIntegerField(
        default=30,
        help_text="Used when customer_cancel_policy is window_minutes.",
    )
    bank_transfer_enabled = models.BooleanField(default=False)
    bank_transfer_instructions = models.TextField(blank=True)
    cash_on_pickup_enabled = models.BooleanField(default=True)
    cash_on_delivery_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Fulfillment<{self.branch_id}>"


class DealSource(models.Model):
    class Kind(models.TextChoices):
        BRAND_LISTING = "brand_listing", "Brand listing page"
        AFFILIATE_FEED = "affiliate_feed", "Affiliate product feed"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="deal_sources"
    )
    name = models.CharField(max_length=120, blank=True)
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.BRAND_LISTING,
    )
    listing_url = models.URLField(max_length=1000, blank=True)
    feed_url = models.URLField(max_length=1000, blank=True)
    is_enabled = models.BooleanField(default=True)
    is_online = models.BooleanField(
        default=True,
        help_text="Imported offers are treated as online (view-only) when true.",
    )
    max_items = models.PositiveIntegerField(
        default=80,
        help_text="Cap product URLs/rows processed per sync run.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        label = self.name or self.listing_url or self.feed_url or f"Source {self.pk}"
        return f"{self.business.name} - {label}"


class Offer(models.Model):
    class OfferType(models.TextChoices):
        PERCENTAGE_BILL = "percentage_bill", "Percentage off entire bill"
        ITEM = "item", "Item or service discount"
        DEAL = "deal", "Deal or bundle"

    DEFAULT_EXTERNAL_URL_LABEL = "View Offer"
    DEFAULT_DEAL_EXTERNAL_URL_LABEL = "View Deal"

    class RedemptionMode(models.TextChoices):
        SCANNABLE = "scannable", "Scannable"
        VIEW_ONLY = "view_only", "View only"

    class UsageLimitType(models.TextChoices):
        ONE_TIME = "one_time", "One time only"
        ONCE_PER_WEEK = "once_per_week", "Once per week"
        ONCE_PER_MONTH = "once_per_month", "Once per month"
        N_TIMES_PER_WEEK = "n_times_per_week", "N times per week"
        N_TIMES_PER_MONTH = "n_times_per_month", "N times per month"
        N_TIMES_TOTAL = "n_times_total", "N times total"

    class Origin(models.TextChoices):
        MANUAL = "manual", "Manual"
        BRAND_LISTING = "brand_listing", "Brand listing"
        AFFILIATE_FEED = "affiliate_feed", "Affiliate feed"

    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class DisabledBy(models.TextChoices):
        SYNC = "sync", "Sync"
        ADMIN = "admin", "Admin"

    class UnavailableReason(models.TextChoices):
        MISSING_FROM_SOURCE = "missing_from_source", "Missing from source"
        HTTP_404 = "http_404", "Product page gone"
        OUT_OF_STOCK = "out_of_stock", "Out of stock"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="offers"
    )
    branches = models.ManyToManyField(Branch, related_name="offers", blank=True)
    offer_type = models.CharField(max_length=20, choices=OfferType.choices)
    redemption_mode = models.CharField(
        max_length=20,
        choices=RedemptionMode.choices,
        default=RedemptionMode.SCANNABLE,
    )
    is_online = models.BooleanField(
        default=False,
        help_text="Offer is available online (no in-store branch required when true).",
    )
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    detailed_description = models.TextField(blank=True)
    external_url = models.URLField(max_length=500, blank=True)
    external_url_label = models.CharField(max_length=80, blank=True)
    image = models.ImageField(upload_to="offer_images/", null=True, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2)
    item_name = models.CharField(max_length=120, blank=True)
    included_items = models.JSONField(
        default=list,
        blank=True,
        help_text="Included item names for deal/bundle offers.",
    )
    original_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    discounted_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    usage_limit_type = models.CharField(max_length=20, choices=UsageLimitType.choices)
    usage_limit_count = models.PositiveIntegerField(default=1)
    is_enabled = models.BooleanField(default=True)
    is_time_limited = models.BooleanField(default=False)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    qr_code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    origin = models.CharField(
        max_length=32,
        choices=Origin.choices,
        default=Origin.MANUAL,
    )
    source = models.ForeignKey(
        "DealSource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offers",
    )
    source_url = models.URLField(max_length=1000, blank=True)
    source_key = models.CharField(max_length=500, blank=True, db_index=True)
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.APPROVED,
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    unavailable_reason = models.CharField(
        max_length=32,
        choices=UnavailableReason.choices,
        blank=True,
    )
    disabled_by = models.CharField(
        max_length=16,
        choices=DisabledBy.choices,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "source_key"],
                condition=models.Q(source_key__gt=""),
                name="unique_offer_source_key_per_business",
            )
        ]
        indexes = [
            models.Index(fields=["review_status", "origin"]),
        ]

    @staticmethod
    def compute_discount_percent(
        original_price: Decimal, discounted_price: Decimal
    ) -> Decimal:
        if original_price <= 0:
            return Decimal("0.00")
        percent = (original_price - discounted_price) / original_price * Decimal("100")
        return percent.quantize(Decimal("0.01"))

    @classmethod
    def default_external_url_label(cls, offer_type: str) -> str:
        if offer_type == cls.OfferType.DEAL:
            return cls.DEFAULT_DEAL_EXTERNAL_URL_LABEL
        return cls.DEFAULT_EXTERNAL_URL_LABEL

    @staticmethod
    def normalize_included_items(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [part.strip() for part in value.replace(",", "\n").splitlines()]
        if not isinstance(value, list):
            return []
        items: list[str] = []
        seen: set[str] = set()
        for raw in value:
            item = str(raw or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    @property
    def is_active(self) -> bool:
        if not self.is_enabled:
            return False
        if not self.is_time_limited:
            return True

        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def __str__(self) -> str:
        return f"{self.business.name} - {self.title}"


class OfferGalleryImage(models.Model):
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="gallery_images"
    )
    image = models.ImageField(upload_to="offer_gallery/", null=True, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"Gallery<{self.offer_id}:{self.id}>"


class OfferBranchStats(models.Model):
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="branch_stats"
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="offer_stats"
    )
    scan_count = models.PositiveIntegerField(default=0)
    avail_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "offer branch stats"
        constraints = [
            models.UniqueConstraint(
                fields=["offer", "branch"], name="unique_offer_branch_stats"
            )
        ]

    def __str__(self) -> str:
        return f"{self.offer.title} @ {self.branch.name}"


class OfferScan(models.Model):
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="scans")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="scans")
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offer_scans",
    )
    bill_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    original_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    amount_to_pay = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scanned_at"]

    def __str__(self) -> str:
        return f"Scan<{self.offer_id}@{self.branch_id}>"


class OfferRedemption(models.Model):
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="redemptions"
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="redemptions"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="offer_redemptions"
    )
    bill_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    original_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    amount_to_pay = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-redeemed_at"]
        indexes = [
            models.Index(
                fields=["user", "offer", "redeemed_at"],
                name="offer_redemp_user_offer_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Redemption<{self.offer_id}@{self.branch_id}>"


class OfferEngagementStats(models.Model):
    offer = models.OneToOneField(
        Offer, on_delete=models.CASCADE, related_name="engagement_stats"
    )
    view_count = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"OfferStats<{self.offer_id}>"


class BusinessEngagementStats(models.Model):
    business = models.OneToOneField(
        Business, on_delete=models.CASCADE, related_name="engagement_stats"
    )
    view_count = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"BusinessStats<{self.business_id}>"


class OfferLike(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="offer_likes"
    )
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "offer"], name="unique_offer_like_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"OfferLike<{self.user_id}:{self.offer_id}>"


class BusinessLike(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="business_likes"
    )
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="likes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "business"], name="unique_business_like_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"BusinessLike<{self.user_id}:{self.business_id}>"


class BranchLike(models.Model):
    """Per-branch favorite. Consumers favorite specific store locations."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="branch_likes"
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="likes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "branch"], name="unique_branch_like_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"BranchLike<{self.user_id}:{self.branch_id}>"


class OfferViewEvent(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="offer_view_events",
        null=True,
        blank=True,
    )
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="view_events"
    )
    viewed_on = models.DateField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "offer", "viewed_on"],
                condition=models.Q(user__isnull=False),
                name="unique_offer_view_per_user_day",
            )
        ]

    def __str__(self) -> str:
        return f"OfferView<{self.offer_id}@{self.viewed_on}>"


class UserPreferences(models.Model):
    class ThemePreference(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="preferences"
    )
    notifications_enabled = models.BooleanField(default=True)
    theme_preference = models.CharField(
        max_length=16,
        choices=ThemePreference.choices,
        default=ThemePreference.SYSTEM,
    )
    preferred_categories = models.ManyToManyField(Category, blank=True)

    def __str__(self) -> str:
        return f"Preferences<{self.user.email}>"


class DeviceToken(models.Model):
    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="device_tokens"
    )
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=16, choices=Platform.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "platform"]),
        ]

    def __str__(self) -> str:
        return f"DeviceToken<{self.user_id}:{self.platform}>"


class Notification(models.Model):
    class NotificationType(models.TextChoices):
        FAVORITED_BUSINESS_NEW_OFFER = (
            "favorited_business_new_offer",
            "Favorited business new offer",
        )
        OFFER_EXPIRING_SOON = ("offer_expiring_soon", "Offer expiring soon")
        REDEMPTION_CONFIRMATION = (
            "redemption_confirmation",
            "Redemption confirmation",
        )
        GENERIC = ("generic", "Generic")

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(
        max_length=64,
        choices=NotificationType.choices,
        default=NotificationType.GENERIC,
    )
    title = models.CharField(max_length=160)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "read_at"]),
        ]

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self) -> None:
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])

    def __str__(self) -> str:
        return f"Notification<{self.user_id}:{self.type}>"


class Address(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="addresses")
    street = models.CharField(max_length=120)
    house_number = models.CharField(max_length=20)
    postal_code = models.CharField(max_length=20)
    city = models.CharField(max_length=80)
    city_ref = models.ForeignKey(
        City,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="addresses",
    )
    county = models.CharField(max_length=80)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "addresses"

    @property
    def formatted_address(self) -> str:
        return f"{self.street} {self.house_number}, {self.postal_code} {self.city}"

    def __str__(self) -> str:
        return self.formatted_address


class PasswordResetToken(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="password_reset_tokens"
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["user", "used_at"]),
        ]

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() < self.expires_at

    def mark_used(self) -> None:
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])

    def __str__(self) -> str:
        return f"PasswordReset<{self.user.email}>"


class Product(models.Model):
    """Catalog item — successor to Offer for commerce flows."""

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="products"
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
    )
    branches = models.ManyToManyField(Branch, related_name="products", blank=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    detailed_description = models.TextField(blank=True)
    image = models.ImageField(upload_to="product_images/", null=True, blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    sale_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    is_available = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    stock_quantity = models.PositiveIntegerField(null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    # Link back to legacy offer when migrated.
    source_offer = models.OneToOneField(
        "Offer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="migrated_product",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "-created_at", "-id"]
        indexes = [
            models.Index(fields=["business", "is_enabled", "is_available"]),
            models.Index(fields=["category"]),
        ]

    @staticmethod
    def compute_discount_percent(
        base_price: Decimal, sale_price: Decimal
    ) -> Decimal:
        if base_price <= 0:
            return Decimal("0.00")
        percent = (base_price - sale_price) / base_price * Decimal("100")
        return percent.quantize(Decimal("0.01"))

    @property
    def has_discount(self) -> bool:
        if self.sale_price is None:
            return False
        return self.sale_price < self.base_price

    @property
    def effective_price(self) -> Decimal:
        if self.has_discount and self.sale_price is not None:
            return self.sale_price
        return self.base_price

    @property
    def effective_discount_percent(self) -> Decimal:
        if not self.has_discount or self.sale_price is None:
            return Decimal("0.00")
        if self.discount_percent is not None:
            return self.discount_percent
        return self.compute_discount_percent(self.base_price, self.sale_price)

    def apply_percent_discount(self, percent: Decimal) -> None:
        percent = max(Decimal("0"), min(Decimal("100"), percent)).quantize(
            Decimal("0.01")
        )
        if percent <= 0:
            self.discount_percent = None
            self.sale_price = None
            return
        self.discount_percent = percent
        factor = (Decimal("100") - percent) / Decimal("100")
        self.sale_price = (self.base_price * factor).quantize(Decimal("0.01"))

    def apply_sale_price(self, sale_price: Decimal) -> None:
        sale_price = sale_price.quantize(Decimal("0.01"))
        if sale_price >= self.base_price:
            self.discount_percent = None
            self.sale_price = None
            return
        self.sale_price = sale_price
        self.discount_percent = self.compute_discount_percent(
            self.base_price, sale_price
        )

    def __str__(self) -> str:
        return f"{self.business.name} - {self.name}"


class ProductGalleryImage(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="gallery_images"
    )
    image = models.ImageField(upload_to="product_gallery/", null=True, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"ProductGallery<{self.product_id}:{self.id}>"


class ProductEngagementStats(models.Model):
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name="engagement_stats"
    )
    view_count = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)
    order_count = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"ProductStats<{self.product_id}>"


class ProductLike(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="product_likes"
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="likes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "product"], name="unique_product_like_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"ProductLike<{self.user_id}:{self.product_id}>"


class ProductViewEvent(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="product_view_events",
        null=True,
        blank=True,
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="view_events"
    )
    viewed_on = models.DateField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "product", "viewed_on"],
                condition=models.Q(user__isnull=False),
                name="unique_product_view_per_user_day",
            )
        ]

    def __str__(self) -> str:
        return f"ProductView<{self.product_id}@{self.viewed_on}>"


class Cart(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="cart")
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Cart<{self.user_id}>"


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="cart_items"
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
    )
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product", "branch"],
                name="unique_cart_product_branch",
            )
        ]

    def __str__(self) -> str:
        return f"CartItem<{self.cart_id}:{self.product_id}x{self.quantity}>"


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        CANCELLED = "cancelled", "Cancelled"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PAYMENT_SUBMITTED = "payment_submitted", "Payment submitted"
        PAID_CONFIRMED = "paid_confirmed", "Payment confirmed"
        PREPARING = "preparing", "Preparing"
        READY_FOR_PICKUP = "ready_for_pickup", "Ready for pickup"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        COMPLETED = "completed", "Completed"

    class FulfillmentType(models.TextChoices):
        PICKUP = "pickup", "In-store pickup"
        LOCAL_SAME_DAY = "local_same_day", "Local / same-day delivery"
        NATIONWIDE = "nationwide", "Nationwide / standard delivery"

    class PaymentMethod(models.TextChoices):
        CASH_ON_PICKUP = "cash_on_pickup", "Cash on pickup"
        CASH_ON_DELIVERY = "cash_on_delivery", "Cash on delivery"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        # Reserved for later gateways
        STRIPE = "stripe", "Stripe"
        JAZZCASH = "jazzcash", "JazzCash"

    class CancelledBy(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        BUSINESS = "business", "Business"
        SYSTEM = "system", "System"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="orders"
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="orders"
    )
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.PENDING
    )
    fulfillment_type = models.CharField(
        max_length=32, choices=FulfillmentType.choices
    )
    payment_method = models.CharField(
        max_length=32, choices=PaymentMethod.choices
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    total = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_address_text = models.TextField(blank=True)
    delivery_city = models.ForeignKey(
        City,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )
    customer_notes = models.TextField(blank=True)
    # Snapshots of cancel policy at place time
    customer_cancel_allowed = models.BooleanField(default=True)
    customer_cancel_until = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.CharField(
        max_length=16, choices=CancelledBy.choices, blank=True
    )
    cancel_reason = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-placed_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-placed_at"]),
            models.Index(fields=["business", "status"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self) -> str:
        return f"Order<{self.public_id}>"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
    )
    product_name = models.CharField(max_length=160)
    unit_base_price = models.DecimalField(max_digits=10, decimal_places=2)
    unit_sale_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    unit_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00")
    )
    quantity = models.PositiveIntegerField(default=1)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"OrderItem<{self.order_id}:{self.product_name}>"


class OrderDeliverySnapshot(models.Model):
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name="delivery_snapshot"
    )
    fulfillment_type = models.CharField(max_length=32)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    max_delivery_hours = models.PositiveIntegerField(null=True, blank=True)
    promised_by = models.DateTimeField(null=True, blank=True)
    branch_city_id = models.IntegerField(null=True, blank=True)
    branch_city_name = models.CharField(max_length=80, blank=True)
    customer_city_id = models.IntegerField(null=True, blank=True)
    customer_city_name = models.CharField(max_length=80, blank=True)
    pickup_radius_km = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    settings_json = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"DeliverySnapshot<{self.order_id}>"


class OrderPaymentProof(models.Model):
    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending review"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="payment_proofs"
    )
    file = models.FileField(upload_to="payment_proofs/")
    note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]

    def __str__(self) -> str:
        return f"PaymentProof<{self.order_id}:{self.id}>"
