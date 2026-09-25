from decimal import Decimal

from rest_framework import serializers

from .delivery_options import get_or_create_fulfillment_settings, option_to_dict, resolve_delivery_options
from .fields import OptionalImageField
from .models import (
    Branch,
    BranchContact,
    BranchFulfillmentSettings,
    Business,
    Cart,
    CartItem,
    Category,
    City,
    Country,
    Order,
    OrderDeliverySnapshot,
    OrderItem,
    OrderPaymentProof,
    Product,
    ProductGalleryImage,
)
from .offer_utils import build_media_url
from .product_pricing import apply_discount_percent, apply_sale_price, clear_discount


class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ["id", "code", "name"]


class CitySerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)
    country_id = serializers.PrimaryKeyRelatedField(
        queryset=Country.objects.all(), source="country", write_only=True, required=False
    )

    class Meta:
        model = City
        fields = ["id", "name", "name_normalized", "country", "country_id"]
        read_only_fields = ["name_normalized"]


class CategoryTreeSerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "parent_id",
            "sort_order",
            "is_active",
            "children",
        ]

    def get_children(self, obj: Category):
        qs = obj.children.filter(is_active=True).order_by("sort_order", "name", "id")
        return CategoryTreeSerializer(qs, many=True, context=self.context).data


class CategoryWriteSerializer(serializers.ModelSerializer):
    parent_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source="parent",
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "parent_id", "sort_order", "is_active"]

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Category name is required.")
        return value

    def validate(self, attrs):
        name = attrs.get("name") or getattr(self.instance, "name", None)
        parent = attrs.get("parent", serializers.empty)
        if parent is serializers.empty:
            parent = getattr(self.instance, "parent", None)
        qs = Category.objects.filter(name=name, parent=parent)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"name": "A category with this name already exists under this parent."}
            )
        return attrs


class BranchContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchContact
        fields = ["id", "contact_type", "value", "is_primary"]

    def validate_value(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Contact value is required.")
        return value


class BranchFulfillmentSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchFulfillmentSettings
        fields = [
            "pickup_enabled",
            "pickup_radius_km",
            "local_same_day_enabled",
            "local_delivery_fee",
            "local_max_delivery_hours",
            "nationwide_enabled",
            "nationwide_delivery_fee",
            "nationwide_max_delivery_hours",
            "customer_cancel_policy",
            "customer_cancel_window_minutes",
            "bank_transfer_enabled",
            "bank_transfer_instructions",
            "cash_on_pickup_enabled",
            "cash_on_delivery_enabled",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class ProductGallerySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductGalleryImage
        fields = ["id", "image_url", "source_url", "sort_order"]

    def get_image_url(self, obj: ProductGalleryImage) -> str | None:
        return build_media_url(self.context.get("request"), obj.image)


class ProductSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), source="category"
    )
    category_name = serializers.CharField(source="category.name", read_only=True)
    branch_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Branch.objects.all(),
        source="branches",
        required=False,
    )
    image = OptionalImageField(required=False, allow_null=True, write_only=True)
    image_url = serializers.SerializerMethodField()
    gallery = ProductGallerySerializer(
        source="gallery_images", many=True, read_only=True
    )
    has_discount = serializers.BooleanField(read_only=True)
    effective_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    effective_discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, read_only=True
    )
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    view_count = serializers.SerializerMethodField()
    like_count = serializers.SerializerMethodField()
    order_count = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "business_id",
            "business_name",
            "category_id",
            "category_name",
            "branch_ids",
            "name",
            "description",
            "detailed_description",
            "image",
            "image_url",
            "gallery",
            "base_price",
            "discount_percent",
            "sale_price",
            "has_discount",
            "effective_price",
            "effective_discount_percent",
            "is_available",
            "is_enabled",
            "stock_quantity",
            "sort_order",
            "view_count",
            "like_count",
            "order_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_at",
            "updated_at",
            "has_discount",
            "effective_price",
            "effective_discount_percent",
        ]

    def get_image_url(self, obj: Product) -> str | None:
        return build_media_url(self.context.get("request"), obj.image)

    def _stats(self, obj: Product):
        return getattr(obj, "engagement_stats", None)

    def get_view_count(self, obj: Product) -> int:
        stats = self._stats(obj)
        return stats.view_count if stats else 0

    def get_like_count(self, obj: Product) -> int:
        stats = self._stats(obj)
        return stats.like_count if stats else 0

    def get_order_count(self, obj: Product) -> int:
        stats = self._stats(obj)
        return stats.order_count if stats else 0

    def validate(self, attrs):
        base = attrs.get("base_price", getattr(self.instance, "base_price", None))
        sale = attrs.get("sale_price", getattr(self.instance, "sale_price", None))
        percent = attrs.get(
            "discount_percent", getattr(self.instance, "discount_percent", None)
        )
        if sale is not None and base is not None and sale > base:
            raise serializers.ValidationError(
                {"sale_price": "Sale price cannot exceed base price."}
            )
        if percent is not None and (percent < 0 or percent > 100):
            raise serializers.ValidationError(
                {"discount_percent": "Discount percent must be between 0 and 100."}
            )
        return attrs

    def create(self, validated_data):
        branches = validated_data.pop("branches", [])
        business = self.context["business"]
        product = Product.objects.create(business=business, **validated_data)
        if branches:
            product.branches.set(branches)
        # Normalize discount fields
        if product.sale_price is not None:
            apply_sale_price(product, product.sale_price)
        elif product.discount_percent is not None:
            apply_discount_percent(product, product.discount_percent)
        return product

    def update(self, instance, validated_data):
        branches = validated_data.pop("branches", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if branches is not None:
            instance.branches.set(branches)
        if "sale_price" in validated_data and validated_data["sale_price"] is not None:
            apply_sale_price(instance, validated_data["sale_price"])
        elif (
            "discount_percent" in validated_data
            and validated_data["discount_percent"] is not None
        ):
            apply_discount_percent(instance, validated_data["discount_percent"])
        elif validated_data.get("sale_price") is None and validated_data.get(
            "discount_percent"
        ) is None:
            if "sale_price" in validated_data or "discount_percent" in validated_data:
                clear_discount(instance)
        return instance


class ProductDiscountSerializer(serializers.Serializer):
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )
    sale_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True
    )
    clear = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs.get("clear"):
            return attrs
        if attrs.get("discount_percent") is None and attrs.get("sale_price") is None:
            raise serializers.ValidationError(
                "Provide discount_percent, sale_price, or clear=true."
            )
        return attrs


class BulkDiscountSerializer(serializers.Serializer):
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    product_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True
    )
    all_products = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if not attrs.get("all_products") and not attrs.get("product_ids"):
            raise serializers.ValidationError(
                "Provide product_ids or set all_products=true."
            )
        percent = attrs["discount_percent"]
        if percent < 0 or percent > 100:
            raise serializers.ValidationError(
                {"discount_percent": "Must be between 0 and 100."}
            )
        return attrs


class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    product_id = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.filter(is_enabled=True),
        source="product",
        write_only=True,
    )
    branch_id = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(),
        source="branch",
        allow_null=True,
        required=False,
    )
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = [
            "id",
            "product",
            "product_id",
            "branch_id",
            "quantity",
            "line_total",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_line_total(self, obj: CartItem) -> str:
        total = (obj.product.effective_price * obj.quantity).quantize(Decimal("0.01"))
        return str(total)


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ["id", "items", "subtotal", "updated_at"]

    def get_subtotal(self, obj: Cart) -> str:
        total = Decimal("0.00")
        for item in obj.items.select_related("product"):
            total += item.product.effective_price * item.quantity
        return str(total.quantize(Decimal("0.01")))


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product_id",
            "product_name",
            "unit_base_price",
            "unit_sale_price",
            "unit_discount_percent",
            "quantity",
            "line_total",
        ]


class OrderDeliverySnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderDeliverySnapshot
        fields = [
            "fulfillment_type",
            "delivery_fee",
            "max_delivery_hours",
            "promised_by",
            "branch_city_id",
            "branch_city_name",
            "customer_city_id",
            "customer_city_name",
            "pickup_radius_km",
            "settings_json",
        ]


class OrderPaymentProofSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = OrderPaymentProof
        fields = [
            "id",
            "file_url",
            "note",
            "submitted_at",
            "review_status",
            "reviewed_at",
            "review_note",
        ]
        read_only_fields = [
            "submitted_at",
            "review_status",
            "reviewed_at",
            "review_note",
        ]

    def get_file_url(self, obj: OrderPaymentProof) -> str | None:
        return build_media_url(self.context.get("request"), obj.file)


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    delivery_snapshot = OrderDeliverySnapshotSerializer(read_only=True)
    payment_proofs = OrderPaymentProofSerializer(many=True, read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    can_customer_cancel = serializers.SerializerMethodField()
    bank_transfer_instructions = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "public_id",
            "business_id",
            "business_name",
            "branch_id",
            "branch_name",
            "status",
            "fulfillment_type",
            "payment_method",
            "subtotal",
            "delivery_fee",
            "total",
            "delivery_address_text",
            "customer_notes",
            "customer_cancel_allowed",
            "customer_cancel_until",
            "can_customer_cancel",
            "cancelled_by",
            "cancel_reason",
            "cancelled_at",
            "placed_at",
            "updated_at",
            "items",
            "delivery_snapshot",
            "payment_proofs",
            "bank_transfer_instructions",
        ]

    def get_can_customer_cancel(self, obj: Order) -> bool:
        from .order_service import customer_can_cancel

        return customer_can_cancel(obj)

    def get_bank_transfer_instructions(self, obj: Order) -> str:
        if obj.payment_method != Order.PaymentMethod.BANK_TRANSFER:
            return ""
        settings = get_or_create_fulfillment_settings(obj.branch)
        return settings.bank_transfer_instructions


class CheckoutGroupSerializer(serializers.Serializer):
    branch_id = serializers.IntegerField()
    item_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    fulfillment_type = serializers.ChoiceField(choices=Order.FulfillmentType.choices)
    payment_method = serializers.ChoiceField(choices=Order.PaymentMethod.choices)
    customer_notes = serializers.CharField(required=False, allow_blank=True, default="")
    delivery_address_text = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class CheckoutPreviewSerializer(serializers.Serializer):
    branch_id = serializers.IntegerField()
    item_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)


class CheckoutPlaceSerializer(serializers.Serializer):
    groups = CheckoutGroupSerializer(many=True)


class OrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Order.Status.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class PaymentProofUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    note = serializers.CharField(required=False, allow_blank=True, default="")


class PaymentProofReviewSerializer(serializers.Serializer):
    review_status = serializers.ChoiceField(
        choices=[
            OrderPaymentProof.ReviewStatus.ACCEPTED,
            OrderPaymentProof.ReviewStatus.REJECTED,
        ]
    )
    review_note = serializers.CharField(required=False, allow_blank=True, default="")


class DeliveryOptionsResponseSerializer(serializers.Serializer):
    options = serializers.ListField(child=serializers.DictField())


def serialize_delivery_options(branch: Branch, location) -> list[dict]:
    return [option_to_dict(opt) for opt in resolve_delivery_options(branch, location)]


class BusinessPresenceSerializer(serializers.ModelSerializer):
    category_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Category.objects.all(),
        source="categories",
        required=False,
    )
    primary_city_id = serializers.PrimaryKeyRelatedField(
        queryset=City.objects.all(),
        source="primary_city",
        allow_null=True,
        required=False,
    )
    primary_country_id = serializers.PrimaryKeyRelatedField(
        queryset=Country.objects.all(),
        source="primary_country",
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Business
        fields = [
            "presence_mode",
            "online_coverage",
            "primary_city_id",
            "primary_country_id",
            "category_ids",
        ]
