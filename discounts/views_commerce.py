from decimal import Decimal
from datetime import timedelta

from django.db.models import Case, Count, F, IntegerField, Prefetch, Q, Sum, When
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .delivery_options import get_or_create_fulfillment_settings
from .category_utils import (
    category_ids_with_products_for_customers,
    product_category_q,
    sync_business_categories,
)
from .geo_utils import get_or_create_city, get_or_create_default_country
from .location_utils import resolve_user_location
from .models import (
    Branch,
    BranchContact,
    Business,
    CartItem,
    Category,
    City,
    Country,
    Order,
    OrderItem,
    OrderPaymentProof,
    OrderProblemReport,
    Product,
    ProductEngagementStats,
    ProductGalleryImage,
    ProductLike,
    ProductReview,
    ProductViewEvent,
)
from .order_service import (
    add_or_update_cart_item,
    cancel_order,
    get_or_create_cart,
    place_orders_from_cart,
    transition_order_status,
)
from .pagination import StandardResultsSetPagination
from .permissions import (
    IsAdminAccount,
    IsBusinessAccount,
    IsConsumerAccount,
    deny_support_write,
)
from .product_pricing import apply_discount_percent, apply_sale_price, bulk_apply_percent, clear_discount
from .serializers_commerce import (
    AdminProductSerializer,
    BranchContactSerializer,
    BranchFulfillmentSettingsSerializer,
    BulkDiscountSerializer,
    BusinessPresenceSerializer,
    CartItemSerializer,
    CartSerializer,
    CategoryTreeSerializer,
    CategoryWriteSerializer,
    CheckoutPlaceSerializer,
    CheckoutPreviewSerializer,
    CitySerializer,
    CountrySerializer,
    OrderCancelSerializer,
    OrderPaymentProofSerializer,
    OrderProblemReportSerializer,
    OrderSerializer,
    OrderStatusUpdateSerializer,
    PaymentProofReviewSerializer,
    PaymentProofUploadSerializer,
    ProductDiscountSerializer,
    ProductGalleryReorderSerializer,
    ProductSerializer,
    serialize_delivery_options,
)
from .visibility import business_is_visible, resolve_business_visibility


def _admin_support_write_blocked(request):
    if deny_support_write(request):
        return Response(
            {"detail": "Owner admin role required for this action."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class UserLocationContextMixin:
    def get_user_location(self):
        return resolve_user_location(self.request)


class CountriesListAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(CountrySerializer(Country.objects.all(), many=True).data)


class CitiesListAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = City.objects.select_related("country").all()
        country_id = request.query_params.get("country_id")
        if country_id:
            qs = qs.filter(country_id=country_id)
        q = request.query_params.get("q")
        if q:
            qs = qs.filter(name__icontains=q.strip())
        return Response(CitySerializer(qs[:100], many=True).data)


class CategoriesTreeAPIView(APIView):
    """
    Nested category tree.

    Query params:
      - populated=1: only categories/subcategories that have sellable products
        (plus ancestors). Used by customer apps so empty shelves stay hidden.
      - without populated: full active tree (merchant pickers / admin tools).
    """

    permission_classes = [AllowAny]

    def get(self, request):
        roots = Category.objects.filter(parent__isnull=True, is_active=True).order_by(
            "sort_order", "name", "id"
        )
        context = {"request": request}
        if request.query_params.get("populated") in ("1", "true", "True"):
            visible = category_ids_with_products_for_customers()
            if not visible:
                return Response([])
            roots = roots.filter(id__in=visible)
            context["visible_ids"] = visible
        return Response(CategoryTreeSerializer(roots, many=True, context=context).data)


class ProductsFeedMixin(UserLocationContextMixin):
    def base_product_qs(self):
        return (
            Product.objects.filter(is_enabled=True, is_available=True)
            .select_related("business", "category", "engagement_stats")
            .prefetch_related("branches", "gallery_images", "branch_engagement_stats")
        )

    def filter_visible_products(self, qs):
        location = self.get_user_location()
        visible_ids = []
        businesses = {
            b.id: b
            for b in Business.objects.filter(
                id__in=qs.values_list("business_id", flat=True).distinct()
            ).prefetch_related("branches")
        }
        for product in qs:
            business = businesses.get(product.business_id)
            if business and business_is_visible(business, location):
                visible_ids.append(product.id)
        return qs.filter(id__in=visible_ids)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        from .review_service import parse_optional_branch_id

        branch_id = parse_optional_branch_id(
            self.request.query_params.get("branch_id")
        )
        if branch_id:
            ctx["branch_id"] = branch_id
        return ctx


class ProductListAPIView(ProductsFeedMixin, generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = ProductSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        from django.db.models import F

        qs = self.base_product_qs()
        category_id = self.request.query_params.get("category_id")
        business_id = self.request.query_params.get("business_id")
        discounted = self.request.query_params.get("discounted")
        q = self.request.query_params.get("q")
        if category_id:
            qs = qs.filter(product_category_q(category_id))
        if business_id:
            qs = qs.filter(business_id=business_id)
        if discounted in ("1", "true", "True"):
            qs = qs.filter(sale_price__isnull=False, sale_price__lt=F("base_price"))
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(business__name__icontains=q)
            )
        qs = self.filter_visible_products(qs)
        return qs.order_by(
            models_discount_nulls_last(),
            "-engagement_stats__view_count",
            "-created_at",
        )


def models_discount_nulls_last():
    from django.db.models import Case, IntegerField, When

    return Case(
        When(sale_price__isnull=False, then=0),
        default=1,
        output_field=IntegerField(),
    )


class ProductDetailAPIView(ProductsFeedMixin, generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = ProductSerializer
    lookup_url_kwarg = "product_id"

    def get_queryset(self):
        return self.base_product_qs()


class ProductViewAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, product_id: int):
        product = get_object_or_404(Product, pk=product_id, is_enabled=True)
        stats, _ = ProductEngagementStats.objects.get_or_create(product=product)
        user = request.user if request.user.is_authenticated else None
        if user:
            _, created = ProductViewEvent.objects.get_or_create(
                user=user, product=product, viewed_on=timezone.localdate()
            )
            if created:
                ProductEngagementStats.objects.filter(pk=stats.pk).update(
                    view_count=stats.view_count + 1
                )
        else:
            ProductEngagementStats.objects.filter(pk=stats.pk).update(
                view_count=stats.view_count + 1
            )
        return Response({"ok": True})


class ProductLikeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, product_id: int):
        product = get_object_or_404(Product, pk=product_id, is_enabled=True)
        like, created = ProductLike.objects.get_or_create(
            user=request.user, product=product
        )
        stats, _ = ProductEngagementStats.objects.get_or_create(product=product)
        if created:
            ProductEngagementStats.objects.filter(pk=stats.pk).update(
                like_count=stats.like_count + 1
            )
        return Response({"liked": True, "created": created})

    def delete(self, request, product_id: int):
        product = get_object_or_404(Product, pk=product_id)
        deleted, _ = ProductLike.objects.filter(
            user=request.user, product=product
        ).delete()
        if deleted:
            stats, _ = ProductEngagementStats.objects.get_or_create(product=product)
            ProductEngagementStats.objects.filter(pk=stats.pk).update(
                like_count=max(0, stats.like_count - 1)
            )
        return Response({"liked": False})


class HomeFeedsAPIView(ProductsFeedMixin, APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = self.filter_visible_products(self.base_product_qs())
        top_picks = qs.order_by("-engagement_stats__like_count", "-engagement_stats__view_count")[:12]
        offers = qs.filter(sale_price__isnull=False).extra(
            where=["sale_price < base_price"]
        ).order_by("-discount_percent", "-created_at")[:12]
        trending = qs.order_by(
            "-engagement_stats__order_count",
            "-engagement_stats__view_count",
            "-created_at",
        )[:12]
        ser = ProductSerializer
        ctx = {"request": request}
        return Response(
            {
                "top_picks": ser(top_picks, many=True, context=ctx).data,
                "offers": ser(offers, many=True, context=ctx).data,
                "trending": ser(trending, many=True, context=ctx).data,
            }
        )


class StoreCatalogAPIView(ProductsFeedMixin, APIView):
    permission_classes = [AllowAny]

    def get(self, request, business_id: int = None, branch_id: int = None):
        location = self.get_user_location()
        if branch_id:
            branch = get_object_or_404(
                Branch.objects.select_related(
                    "business", "business__engagement_stats", "engagement_stats"
                ).prefetch_related("contacts"),
                pk=branch_id,
            )
            business = branch.business
        else:
            business = get_object_or_404(
                Business.objects.select_related("engagement_stats"), pk=business_id
            )
            branch = (
                business.branches.select_related("engagement_stats")
                .order_by("id")
                .first()
            )

        channels = resolve_business_visibility(business, location)
        if not (channels.show_online or channels.show_instore):
            return Response(
                {"detail": "Business is not available in your area."},
                status=status.HTTP_404_NOT_FOUND,
            )

        qs = self.base_product_qs().filter(business=business)
        if branch:
            qs = qs.annotate(_branch_count=Count("branches")).filter(
                Q(_branch_count=0) | Q(branches=branch)
            ).distinct()

        discounted = list(
            qs.filter(sale_price__isnull=False)
            .extra(where=["sale_price < base_price"])
            .order_by("-discount_percent", "name")
        )
        discounted_ids = {p.id for p in discounted}
        rest = qs.exclude(id__in=discounted_ids).order_by("category__name", "name")

        by_category: dict[str, list] = {}
        for product in rest:
            key = product.category.name
            by_category.setdefault(key, []).append(product)

        from .review_service import branch_rating_payload

        ctx = {"request": request}
        if branch:
            ctx["branch_id"] = branch.id
        contacts = []
        if branch:
            contacts = BranchContactSerializer(branch.contacts.all(), many=True).data

        delivery_options = []
        if branch and location:
            delivery_options = serialize_delivery_options(branch, location)

        branch_ratings = branch_rating_payload(branch)

        return Response(
            {
                "business": {
                    "id": business.id,
                    "name": business.name,
                    "presence_mode": business.presence_mode,
                    "online_coverage": business.online_coverage,
                    "show_online": channels.show_online,
                    "show_instore": channels.show_instore,
                    "rating_avg": str(
                        getattr(
                            getattr(business, "engagement_stats", None),
                            "rating_avg",
                            "0.00",
                        )
                        or "0.00"
                    ),
                    "rating_count": int(
                        getattr(
                            getattr(business, "engagement_stats", None),
                            "rating_count",
                            0,
                        )
                        or 0
                    ),
                },
                "branch": (
                    {
                        "id": branch.id,
                        "name": branch.name,
                        "city": branch.city,
                        "latitude": branch.latitude,
                        "longitude": branch.longitude,
                        "formatted_address": branch.formatted_address,
                        "rating_avg": branch_ratings["rating_avg"],
                        "rating_count": branch_ratings["rating_count"],
                    }
                    if branch
                    else None
                ),
                "contacts": contacts,
                "delivery_options": delivery_options,
                "discounted": ProductSerializer(discounted, many=True, context=ctx).data,
                "categories": [
                    {
                        "category_id": products[0].category_id,
                        "category_name": name,
                        "products": ProductSerializer(
                            products, many=True, context=ctx
                        ).data,
                    }
                    for name, products in by_category.items()
                ],
            }
        )


class BranchDeliveryOptionsAPIView(UserLocationContextMixin, APIView):
    permission_classes = [AllowAny]

    def get(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        location = self.get_user_location()
        return Response(
            {"options": serialize_delivery_options(branch, location)}
        )


# ---- Cart / Checkout / Orders (consumer) ----


class CartAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def get(self, request):
        cart = get_or_create_cart(request.user)
        return Response(
            CartSerializer(cart, context={"request": request}).data
        )

    def delete(self, request):
        cart = get_or_create_cart(request.user)
        cart.items.all().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CartItemListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request):
        cart = get_or_create_cart(request.user)
        serializer = CartItemSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        product = serializer.validated_data["product"]
        branch = serializer.validated_data.get("branch")
        quantity = serializer.validated_data.get("quantity", 1)
        item = add_or_update_cart_item(cart, product, quantity, branch)
        return Response(
            CartItemSerializer(item, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class CartItemDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def patch(self, request, item_id: int):
        cart = get_or_create_cart(request.user)
        item = get_object_or_404(CartItem, pk=item_id, cart=cart)
        quantity = int(request.data.get("quantity", item.quantity))
        if quantity < 1:
            item.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
        return Response(CartItemSerializer(item, context={"request": request}).data)

    def delete(self, request, item_id: int):
        cart = get_or_create_cart(request.user)
        item = get_object_or_404(CartItem, pk=item_id, cart=cart)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckoutPreviewAPIView(UserLocationContextMixin, APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request):
        serializer = CheckoutPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = get_or_create_cart(request.user)
        branch = get_object_or_404(Branch, pk=serializer.validated_data["branch_id"])
        items = list(
            cart.items.filter(id__in=serializer.validated_data["item_ids"]).select_related(
                "product"
            )
        )
        subtotal = sum(
            (i.product.effective_price * i.quantity for i in items), Decimal("0.00")
        )
        location = self.get_user_location()
        options = serialize_delivery_options(branch, location)
        settings = get_or_create_fulfillment_settings(branch)
        return Response(
            {
                "branch_id": branch.id,
                "subtotal": str(subtotal.quantize(Decimal("0.01"))),
                "options": options,
                "payment_methods": {
                    "cash_on_pickup": settings.cash_on_pickup_enabled,
                    "cash_on_delivery": settings.cash_on_delivery_enabled,
                    "bank_transfer": settings.bank_transfer_enabled,
                    "bank_transfer_instructions": settings.bank_transfer_instructions,
                    "stripe": settings.stripe_enabled,
                    "stripe_instructions": settings.stripe_instructions,
                    "jazzcash": settings.jazzcash_enabled or settings.easypaisa_enabled,
                    "jazzcash_instructions": settings.jazzcash_instructions,
                    "easypaisa": settings.easypaisa_enabled,
                    "easypaisa_instructions": settings.easypaisa_instructions,
                },
            }
        )


class CheckoutPlaceAPIView(UserLocationContextMixin, APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout"

    def post(self, request):
        import json

        raw_groups = request.data.get("groups")
        if isinstance(raw_groups, str):
            try:
                raw_groups = json.loads(raw_groups)
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid groups JSON."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            data = {
                "groups": raw_groups,
                "customer_phone": request.data.get("customer_phone") or "",
            }
        else:
            data = request.data

        serializer = CheckoutPlaceSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        proof_files: dict[int, object] = {}
        for key in request.data.keys():
            if not isinstance(key, str):
                continue
            if key.startswith("proof_") or key.startswith("payment_proof_"):
                suffix = key.split("_")[-1]
                if suffix.isdigit():
                    proof_files[int(suffix)] = request.data.get(key)

        # Also accept a single proof file when there is only one group.
        if not proof_files and request.data.get("payment_proof") is not None:
            proof_files[0] = request.data.get("payment_proof")

        cart = get_or_create_cart(request.user)
        orders = place_orders_from_cart(
            user=request.user,
            cart=cart,
            groups=serializer.validated_data["groups"],
            location=self.get_user_location(),
            customer_phone=serializer.validated_data.get("customer_phone") or None,
            proof_files=proof_files,
        )
        from .notification_utils import notify_business_new_order, notify_business_payment_proof

        for order in orders:
            notify_business_new_order(order)
            if order.payment_proofs.exists():
                notify_business_payment_proof(order)
        return Response(
            OrderSerializer(orders, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ConsumerOrderListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    serializer_class = OrderSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = (
            Order.objects.filter(user=self.request.user)
            .select_related("business", "branch", "user")
            .prefetch_related(
                "items",
                "payment_proofs",
                "delivery_snapshot",
                "branch__contacts",
            )
        )
        status_filter = (self.request.query_params.get("status_group") or "").strip().lower()
        if status_filter == "active":
            qs = qs.exclude(
                status__in=[Order.Status.COMPLETED, Order.Status.CANCELLED]
            )
        elif status_filter == "completed":
            qs = qs.filter(status=Order.Status.COMPLETED)
        elif status_filter == "cancelled":
            qs = qs.filter(status=Order.Status.CANCELLED)
        return qs


class ConsumerOrderDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    serializer_class = OrderSerializer
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .select_related("business", "branch", "user")
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=OrderItem.objects.select_related("product").prefetch_related(
                        "review__images"
                    ),
                ),
                "payment_proofs",
                "delivery_snapshot",
                "branch__contacts",
            )
        )


class ConsumerOrderCancelAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        serializer = OrderCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cancel_order(
            order,
            by=Order.CancelledBy.CUSTOMER,
            reason=serializer.validated_data.get("reason") or "",
        )
        return Response(OrderSerializer(order, context={"request": request}).data)


class ConsumerOrderProblemReportAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        serializer = OrderProblemReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = OrderProblemReport.objects.create(
            order=order,
            user=request.user,
            message=serializer.validated_data["message"].strip(),
        )
        return Response(
            {
                "id": report.id,
                "order_id": str(order.public_id),
                "message": report.message,
                "created_at": report.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class ConsumerPaymentProofAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, public_id):
        from .order_service import requires_payment_proof

        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        if not requires_payment_proof(order.payment_method):
            return Response(
                {"detail": "Payment proof only applies to bank transfer, card, or mobile wallet orders."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.status not in (
            Order.Status.AWAITING_PAYMENT,
            Order.Status.ACCEPTED,
            Order.Status.PAYMENT_SUBMITTED,
            Order.Status.PENDING,
        ):
            return Response(
                {"detail": "Order is not awaiting payment proof."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = PaymentProofUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proof = OrderPaymentProof.objects.create(
            order=order,
            file=serializer.validated_data["file"],
            note=serializer.validated_data.get("note") or "",
        )
        if order.status == Order.Status.PENDING:
            # Already attached at place time path — keep pending for merchant accept.
            order.payment_status = Order.PaymentStatus.AWAITING_CONFIRMATION
            order.save(update_fields=["payment_status", "updated_at"])
        else:
            order.status = Order.Status.PAYMENT_SUBMITTED
            order.payment_status = Order.PaymentStatus.AWAITING_CONFIRMATION
            order.save(update_fields=["status", "payment_status", "updated_at"])
        from .notification_utils import notify_business_payment_proof

        notify_business_payment_proof(order)
        return Response(
            OrderPaymentProofSerializer(proof, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ---- Business product / order / settings APIs ----


class BusinessProductListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]
    serializer_class = ProductSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = (
            Product.objects.filter(business=self.request.user.business_profile)
            .select_related("category", "engagement_stats")
            .prefetch_related("branches", "gallery_images")
            .order_by("sort_order", "-created_at")
        )
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(description__icontains=search)
                | Q(category__name__icontains=search)
            )
        enabled = self.request.query_params.get("is_enabled")
        if enabled in ("true", "1"):
            qs = qs.filter(is_enabled=True)
        elif enabled in ("false", "0"):
            qs = qs.filter(is_enabled=False)
        discounted = self.request.query_params.get("has_discount")
        if discounted in ("true", "1"):
            qs = qs.filter(sale_price__isnull=False).extra(
                where=["sale_price < base_price"]
            )
        elif discounted in ("false", "0"):
            qs = qs.filter(
                Q(sale_price__isnull=True) | Q(sale_price__gte=F("base_price"))
            )
        low_stock = self.request.query_params.get("low_stock")
        if low_stock in ("true", "1"):
            qs = qs.filter(
                stock_quantity__isnull=False,
                stock_quantity__lte=ProductSerializer.LOW_STOCK_THRESHOLD,
            )
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["business"] = self.request.user.business_profile
        return ctx

    def perform_create(self, serializer):
        serializer.save()


class BusinessProductDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]
    serializer_class = ProductSerializer
    lookup_url_kwarg = "product_id"
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return Product.objects.filter(business=self.request.user.business_profile)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["business"] = self.request.user.business_profile
        return ctx


class BusinessProductGalleryDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def delete(self, request, product_id: int, image_id: int):
        product = get_object_or_404(
            Product, pk=product_id, business=request.user.business_profile
        )
        image = get_object_or_404(ProductGalleryImage, pk=image_id, product=product)
        image.delete()
        return Response(
            ProductSerializer(product, context={"request": request}).data
        )


class BusinessProductGalleryReorderAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, product_id: int):
        product = get_object_or_404(
            Product, pk=product_id, business=request.user.business_profile
        )
        serializer = ProductGalleryReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image_ids = serializer.validated_data["image_ids"]
        existing = {
            img.id: img
            for img in ProductGalleryImage.objects.filter(product=product)
        }
        if set(image_ids) != set(existing.keys()):
            return Response(
                {
                    "message": "image_ids must include every gallery image exactly once."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        for index, image_id in enumerate(image_ids):
            img = existing[image_id]
            if img.sort_order != index:
                img.sort_order = index
                img.save(update_fields=["sort_order"])
        product.refresh_from_db()
        return Response(
            ProductSerializer(product, context={"request": request}).data
        )


class BusinessProductDiscountAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, product_id: int):
        product = get_object_or_404(
            Product, pk=product_id, business=request.user.business_profile
        )
        serializer = ProductDiscountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("clear"):
            clear_discount(product)
        elif data.get("sale_price") is not None:
            apply_sale_price(product, data["sale_price"])
        else:
            apply_discount_percent(product, data["discount_percent"])
        return Response(
            ProductSerializer(product, context={"request": request}).data
        )


class BusinessBulkDiscountAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request):
        business = request.user.business_profile
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        data["business_id"] = business.id
        serializer = BulkDiscountSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        qs = Product.objects.filter(business=business)
        if not serializer.validated_data.get("all_products"):
            qs = qs.filter(id__in=serializer.validated_data["product_ids"])
        count = bulk_apply_percent(qs, serializer.validated_data["discount_percent"])
        return Response({"updated": count})


class BusinessPresenceAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        business = request.user.business_profile
        return Response(BusinessPresenceSerializer(business).data)

    def patch(self, request):
        business = request.user.business_profile
        serializer = BusinessPresenceSerializer(
            business, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        categories = serializer.validated_data.pop("categories", None)
        for key, value in serializer.validated_data.items():
            setattr(business, key, value)
        business.save()
        if categories is not None:
            sync_business_categories(business, categories=list(categories))
        return Response(BusinessPresenceSerializer(business).data)


class BusinessBranchContactsAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request, branch_id: int):
        branch = get_object_or_404(
            Branch, pk=branch_id, business=request.user.business_profile
        )
        return Response(
            BranchContactSerializer(branch.contacts.all(), many=True).data
        )

    def put(self, request, branch_id: int):
        branch = get_object_or_404(
            Branch, pk=branch_id, business=request.user.business_profile
        )
        serializer = BranchContactSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(
                {"detail": "At least one contact is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        branch.contacts.all().delete()
        created = [
            BranchContact.objects.create(branch=branch, **item)
            for item in serializer.validated_data
        ]
        return Response(BranchContactSerializer(created, many=True).data)


class BusinessBranchFulfillmentAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request, branch_id: int):
        branch = get_object_or_404(
            Branch, pk=branch_id, business=request.user.business_profile
        )
        settings = get_or_create_fulfillment_settings(branch)
        return Response(BranchFulfillmentSettingsSerializer(settings).data)

    def patch(self, request, branch_id: int):
        branch = get_object_or_404(
            Branch, pk=branch_id, business=request.user.business_profile
        )
        settings = get_or_create_fulfillment_settings(branch)
        serializer = BranchFulfillmentSettingsSerializer(
            settings, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BusinessOrderListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]
    serializer_class = OrderSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = (
            Order.objects.filter(business=self.request.user.business_profile)
            .select_related("business", "branch", "user")
            .prefetch_related(
                "items",
                "payment_proofs",
                "delivery_snapshot",
                "branch__contacts",
            )
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(public_id__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__phone__icontains=search)
                | Q(customer_phone__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(delivery_address_text__icontains=search)
                | Q(branch__name__icontains=search)
            )
        date_from = (self.request.query_params.get("date_from") or "").strip()
        date_to = (self.request.query_params.get("date_to") or "").strip()
        if date_from:
            qs = qs.filter(placed_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(placed_at__date__lte=date_to)
        # Same-day Lahore orders float to the top of the merchant queue.
        return qs.annotate(
            same_day_rank=Case(
                When(
                    fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                    then=0,
                ),
                default=1,
                output_field=IntegerField(),
            )
        ).order_by("same_day_rank", "-placed_at")


class BusinessOrderDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]
    serializer_class = OrderSerializer
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"

    def get_queryset(self):
        return (
            Order.objects.filter(business=self.request.user.business_profile)
            .select_related("business", "branch", "user")
            .prefetch_related(
                "items",
                "payment_proofs",
                "delivery_snapshot",
                "branch__contacts",
            )
        )


class BusinessOrderStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, public_id):
        order = get_object_or_404(
            Order, public_id=public_id, business=request.user.business_profile
        )
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["status"] == Order.Status.CANCELLED:
            cancel_order(
                order,
                by=Order.CancelledBy.BUSINESS,
                reason=serializer.validated_data.get("reason") or "",
            )
        else:
            transition_order_status(order, serializer.validated_data["status"])
        return Response(OrderSerializer(order, context={"request": request}).data)


class BusinessPaymentProofReviewAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, public_id, proof_id: int):
        order = get_object_or_404(
            Order, public_id=public_id, business=request.user.business_profile
        )
        proof = get_object_or_404(OrderPaymentProof, pk=proof_id, order=order)
        serializer = PaymentProofReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proof.review_status = serializer.validated_data["review_status"]
        proof.review_note = serializer.validated_data.get("review_note") or ""
        proof.reviewed_at = timezone.now()
        proof.save(
            update_fields=["review_status", "review_note", "reviewed_at"]
        )
        if proof.review_status == OrderPaymentProof.ReviewStatus.ACCEPTED:
            order.status = Order.Status.PAID_CONFIRMED
            order.payment_status = Order.PaymentStatus.PAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
            from .notification_utils import notify_customer_order_status

            notify_customer_order_status(order)
        elif proof.review_status == OrderPaymentProof.ReviewStatus.REJECTED:
            order.status = Order.Status.AWAITING_PAYMENT
            order.payment_status = Order.PaymentStatus.UNPAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
            from .notification_utils import notify_customer_order_status

            notify_customer_order_status(order)
        return Response(
            OrderPaymentProofSerializer(proof, context={"request": request}).data
        )


class BusinessStatsAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        business = request.user.business_profile
        from .review_service import ensure_business_engagement_stats

        biz_stats = ensure_business_engagement_stats(business)
        orders = Order.objects.filter(business=business).exclude(
            status=Order.Status.CANCELLED
        )
        completed = orders.filter(status=Order.Status.COMPLETED)
        totals = completed.aggregate(
            gmv=Sum("total"), order_count=Count("id")
        )
        by_status = (
            Order.objects.filter(business=business)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        )
        by_branch = (
            completed.values("branch_id", "branch__name")
            .annotate(gmv=Sum("total"), order_count=Count("id"))
            .order_by("-gmv")
        )
        by_product = (
            completed.values("items__product_id", "items__product_name")
            .annotate(
                quantity=Sum("items__quantity"),
                gmv=Sum("items__line_total"),
            )
            .order_by("-gmv")[:20]
        )
        return Response(
            {
                "total_orders": Order.objects.filter(business=business).count(),
                "completed_orders": totals["order_count"] or 0,
                "gmv": str(totals["gmv"] or Decimal("0.00")),
                "by_status": list(by_status),
                "by_branch": [
                    {
                        "branch_id": row["branch_id"],
                        "branch_name": row["branch__name"],
                        "gmv": str(row["gmv"] or Decimal("0.00")),
                        "order_count": row["order_count"],
                    }
                    for row in by_branch
                ],
                "by_product": [
                    {
                        "product_id": row["items__product_id"],
                        "product_name": row["items__product_name"],
                        "quantity": row["quantity"] or 0,
                        "gmv": str(row["gmv"] or Decimal("0.00")),
                    }
                    for row in by_product
                ],
                "product_count": Product.objects.filter(business=business).count(),
                "active_product_count": Product.objects.filter(
                    business=business, is_enabled=True, is_available=True
                ).count(),
                "low_stock_count": Product.objects.filter(
                    business=business,
                    stock_quantity__isnull=False,
                    stock_quantity__lte=ProductSerializer.LOW_STOCK_THRESHOLD,
                ).count(),
                "low_stock_threshold": ProductSerializer.LOW_STOCK_THRESHOLD,
                "rating_avg": str(biz_stats.rating_avg),
                "rating_count": biz_stats.rating_count,
            }
        )


class BusinessStatsTimeseriesAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        business = request.user.business_profile
        try:
            days = min(max(int(request.query_params.get("days", 30)), 1), 90)
        except (TypeError, ValueError):
            days = 30

        today = timezone.localdate()
        start = today - timedelta(days=days - 1)
        base = Order.objects.filter(business=business, placed_at__date__gte=start)

        orders_by_day = {
            row["day"]: row["count"]
            for row in base.annotate(day=TruncDate("placed_at"))
            .values("day")
            .annotate(count=Count("id"))
        }
        completed = base.filter(status=Order.Status.COMPLETED)
        gmv_by_day = {
            row["day"]: row["gmv"] or Decimal("0.00")
            for row in completed.annotate(day=TruncDate("placed_at"))
            .values("day")
            .annotate(gmv=Sum("total"))
        }
        completed_by_day = {
            row["day"]: row["count"]
            for row in completed.annotate(day=TruncDate("placed_at"))
            .values("day")
            .annotate(count=Count("id"))
        }

        series = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            series.append(
                {
                    "date": day.isoformat(),
                    "orders": int(orders_by_day.get(day, 0)),
                    "completed": int(completed_by_day.get(day, 0)),
                    "gmv": str(gmv_by_day.get(day, Decimal("0.00"))),
                }
            )
        return Response({"days": days, "series": series})


# ---- Business notifications ----


class BusinessNotificationListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        from .models import Notification
        from .serializers import NotificationSerializer
        from .views import _paginate_notifications

        qs = Notification.objects.filter(user=request.user)
        return _paginate_notifications(qs, request)


class BusinessNotificationUnreadCountAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        from .models import Notification

        count = Notification.objects.filter(
            user=request.user, read_at__isnull=True
        ).count()
        return Response({"unread_count": count})


class BusinessNotificationMarkReadAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, notification_id):
        from .models import Notification
        from .serializers import NotificationSerializer

        notification = get_object_or_404(
            Notification, pk=notification_id, user=request.user
        )
        notification.mark_read()
        return Response(NotificationSerializer(notification).data)


class BusinessNotificationMarkAllReadAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request):
        from .models import Notification

        updated = Notification.objects.filter(
            user=request.user, read_at__isnull=True
        ).update(read_at=timezone.now())
        return Response(
            {"message": "All notifications marked as read.", "updated": updated}
        )


# ---- Admin category tree ----


class AdminCategoryTreeListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def get(self, request):
        roots = Category.objects.filter(parent__isnull=True).order_by(
            "sort_order", "name", "id"
        )
        return Response(
            CategoryTreeSerializer(
                roots,
                many=True,
                context={"request": request, "include_inactive": True, "with_counts": True},
            ).data
        )

    def post(self, request):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        serializer = CategoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return Response(
            CategoryWriteSerializer(category).data, status=status.HTTP_201_CREATED
        )


class AdminCategoryTreeDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def patch(self, request, category_id: int):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        category = get_object_or_404(Category, pk=category_id)
        serializer = CategoryWriteSerializer(
            category, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, category_id: int):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        category = get_object_or_404(Category, pk=category_id)
        if category.children.exists():
            return Response(
                {"detail": "Remove or reassign child categories first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if category.products.exists() or category.businesses.exists() or category.primary_businesses.exists():
            return Response(
                {"detail": "Category is in use."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        category.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminOrderListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]
    serializer_class = OrderSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = (
            Order.objects.select_related("business", "branch", "user")
            .prefetch_related(
                "items",
                "payment_proofs",
                "delivery_snapshot",
                "status_history__actor",
            )
            .order_by("-placed_at")
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        business_id = self.request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(public_id__icontains=search)
                | Q(business__name__icontains=search)
                | Q(branch__name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__phone__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(delivery_address_text__icontains=search)
            )
        date_from = (self.request.query_params.get("date_from") or "").strip()
        date_to = (self.request.query_params.get("date_to") or "").strip()
        if date_from:
            qs = qs.filter(placed_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(placed_at__date__lte=date_to)
        return qs


class AdminOrderDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]
    serializer_class = OrderSerializer
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"

    def get_queryset(self):
        return (
            Order.objects.select_related("business", "branch", "user")
            .prefetch_related(
                "items",
                "payment_proofs",
                "delivery_snapshot",
                "status_history__actor",
            )
        )

    def patch(self, request, *args, **kwargs):
        from .views_admin_trust import AdminOrderPatchAPIView

        return AdminOrderPatchAPIView().patch(request, public_id=kwargs["public_id"])


class AdminOrderStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id)
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["status"] == Order.Status.CANCELLED:
            cancel_order(
                order,
                by=Order.CancelledBy.BUSINESS,
                reason=serializer.validated_data.get("reason") or "",
                actor=request.user,
            )
        else:
            transition_order_status(
                order,
                serializer.validated_data["status"],
                actor=request.user,
            )
        return Response(OrderSerializer(order, context={"request": request}).data)


class AdminPaymentProofReviewAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, public_id, proof_id: int):
        order = get_object_or_404(Order, public_id=public_id)
        proof = get_object_or_404(OrderPaymentProof, pk=proof_id, order=order)
        serializer = PaymentProofReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proof.review_status = serializer.validated_data["review_status"]
        proof.review_note = serializer.validated_data.get("review_note") or ""
        proof.reviewed_at = timezone.now()
        proof.save(
            update_fields=["review_status", "review_note", "reviewed_at"]
        )
        if proof.review_status == OrderPaymentProof.ReviewStatus.ACCEPTED:
            order.status = Order.Status.PAID_CONFIRMED
            order.payment_status = Order.PaymentStatus.PAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
            from .notification_utils import notify_customer_order_status

            notify_customer_order_status(order)
        elif proof.review_status == OrderPaymentProof.ReviewStatus.REJECTED:
            order.status = Order.Status.AWAITING_PAYMENT
            order.payment_status = Order.PaymentStatus.UNPAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
            from .notification_utils import notify_customer_order_status

            notify_customer_order_status(order)
        return Response(
            OrderPaymentProofSerializer(proof, context={"request": request}).data
        )


class AdminProductListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]
    serializer_class = AdminProductSerializer
    pagination_class = StandardResultsSetPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def create(self, request, *args, **kwargs):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        return super().create(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            Product.objects.select_related("business", "category", "engagement_stats")
            .prefetch_related("branches", "gallery_images")
            .order_by("-created_at")
        )
        business_id = self.request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(business__name__icontains=search))
        low_stock = self.request.query_params.get("low_stock")
        if low_stock in ("true", "1"):
            qs = qs.filter(
                stock_quantity__isnull=False,
                stock_quantity__lte=ProductSerializer.LOW_STOCK_THRESHOLD,
            )
        enabled = self.request.query_params.get("is_enabled")
        if enabled in ("true", "1"):
            qs = qs.filter(is_enabled=True)
        elif enabled in ("false", "0"):
            qs = qs.filter(is_enabled=False)
        return qs


class AdminProductDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]
    serializer_class = AdminProductSerializer
    lookup_url_kwarg = "product_id"
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def update(self, request, *args, **kwargs):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        blocked = _admin_support_write_blocked(request)
        if blocked:
            return blocked
        return super().destroy(request, *args, **kwargs)

    def get_queryset(self):
        return Product.objects.select_related("business", "category").prefetch_related(
            "branches", "gallery_images"
        )


class AdminProductGalleryDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def delete(self, request, product_id: int, image_id: int):
        product = get_object_or_404(Product, pk=product_id)
        image = get_object_or_404(ProductGalleryImage, pk=image_id, product=product)
        image.delete()
        return Response(
            AdminProductSerializer(product, context={"request": request}).data
        )


class AdminProductGalleryReorderAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, product_id: int):
        product = get_object_or_404(Product, pk=product_id)
        serializer = ProductGalleryReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image_ids = serializer.validated_data["image_ids"]
        existing = {
            img.id: img
            for img in ProductGalleryImage.objects.filter(product=product)
        }
        if set(image_ids) != set(existing.keys()):
            return Response(
                {
                    "message": "image_ids must include every gallery image exactly once."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        for index, image_id in enumerate(image_ids):
            img = existing[image_id]
            if img.sort_order != index:
                img.sort_order = index
                img.save(update_fields=["sort_order"])
        product.refresh_from_db()
        return Response(
            AdminProductSerializer(product, context={"request": request}).data
        )


class AdminBulkDiscountAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request):
        from .permissions import admin_can_mutate_platform
        from .audit_utils import write_audit

        if not admin_can_mutate_platform(request.user):
            return Response(
                {"detail": "Owner admin role required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = BulkDiscountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        qs = Product.objects.all()
        business_id = serializer.validated_data.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        if not serializer.validated_data.get("all_products"):
            qs = qs.filter(id__in=serializer.validated_data["product_ids"])
        count = bulk_apply_percent(qs, serializer.validated_data["discount_percent"])
        write_audit(
            actor=request.user,
            action="product.bulk_discount",
            target_type="Business" if business_id else "Product",
            target_id=business_id or "",
            metadata={
                "updated": count,
                "discount_percent": str(serializer.validated_data["discount_percent"]),
                "all_products": bool(serializer.validated_data.get("all_products")),
                "product_ids": serializer.validated_data.get("product_ids") or [],
            },
        )
        return Response({"updated": count})


class EnsureGeoSeedAPIView(APIView):
    """Dev helper — ensure default country exists."""

    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request):
        country = get_or_create_default_country()
        return Response(CountrySerializer(country).data)
