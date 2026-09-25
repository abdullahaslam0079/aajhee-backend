from decimal import Decimal

from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .delivery_options import get_or_create_fulfillment_settings
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
    OrderPaymentProof,
    Product,
    ProductEngagementStats,
    ProductLike,
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
from .permissions import IsAdminAccount, IsBusinessAccount, IsConsumerAccount
from .product_pricing import apply_discount_percent, apply_sale_price, bulk_apply_percent, clear_discount
from .serializers_commerce import (
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
    OrderPaymentProofSerializer,
    OrderSerializer,
    OrderStatusUpdateSerializer,
    PaymentProofReviewSerializer,
    PaymentProofUploadSerializer,
    ProductDiscountSerializer,
    ProductSerializer,
    serialize_delivery_options,
)
from .visibility import business_is_visible, resolve_business_visibility


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
    permission_classes = [AllowAny]

    def get(self, request):
        roots = Category.objects.filter(parent__isnull=True, is_active=True).order_by(
            "sort_order", "name", "id"
        )
        return Response(CategoryTreeSerializer(roots, many=True).data)


class ProductsFeedMixin(UserLocationContextMixin):
    def base_product_qs(self):
        return (
            Product.objects.filter(is_enabled=True, is_available=True)
            .select_related("business", "category", "engagement_stats")
            .prefetch_related("branches", "gallery_images")
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
            qs = qs.filter(
                Q(category_id=category_id) | Q(category__parent_id=category_id)
            )
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
                Branch.objects.select_related("business").prefetch_related("contacts"),
                pk=branch_id,
            )
            business = branch.business
        else:
            business = get_object_or_404(Business, pk=business_id)
            branch = business.branches.order_by("id").first()

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

        ctx = {"request": request}
        contacts = []
        if branch:
            contacts = BranchContactSerializer(branch.contacts.all(), many=True).data

        delivery_options = []
        if branch and location:
            delivery_options = serialize_delivery_options(branch, location)

        return Response(
            {
                "business": {
                    "id": business.id,
                    "name": business.name,
                    "presence_mode": business.presence_mode,
                    "online_coverage": business.online_coverage,
                    "show_online": channels.show_online,
                    "show_instore": channels.show_instore,
                },
                "branch": (
                    {
                        "id": branch.id,
                        "name": branch.name,
                        "city": branch.city,
                        "latitude": branch.latitude,
                        "longitude": branch.longitude,
                        "formatted_address": branch.formatted_address,
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
                },
            }
        )


class CheckoutPlaceAPIView(UserLocationContextMixin, APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request):
        serializer = CheckoutPlaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = get_or_create_cart(request.user)
        orders = place_orders_from_cart(
            user=request.user,
            cart=cart,
            groups=serializer.validated_data["groups"],
            location=self.get_user_location(),
        )
        return Response(
            OrderSerializer(orders, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ConsumerOrderListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    serializer_class = OrderSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .select_related("business", "branch")
            .prefetch_related("items", "payment_proofs", "delivery_snapshot")
        )


class ConsumerOrderDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    serializer_class = OrderSerializer
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .select_related("business", "branch")
            .prefetch_related("items", "payment_proofs", "delivery_snapshot")
        )


class ConsumerOrderCancelAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]

    def post(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        reason = request.data.get("reason", "")
        cancel_order(order, by=Order.CancelledBy.CUSTOMER, reason=reason)
        return Response(OrderSerializer(order, context={"request": request}).data)


class ConsumerPaymentProofAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        if order.payment_method != Order.PaymentMethod.BANK_TRANSFER:
            return Response(
                {"detail": "Payment proof only applies to bank transfer orders."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.status not in (
            Order.Status.AWAITING_PAYMENT,
            Order.Status.ACCEPTED,
            Order.Status.PAYMENT_SUBMITTED,
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
        order.status = Order.Status.PAYMENT_SUBMITTED
        order.save(update_fields=["status", "updated_at"])
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
        return (
            Product.objects.filter(business=self.request.user.business_profile)
            .select_related("category", "engagement_stats")
            .prefetch_related("branches", "gallery_images")
            .order_by("sort_order", "-created_at")
        )

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
        serializer = BulkDiscountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        business = request.user.business_profile
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
            business.categories.set(categories)
            if categories and not business.category_id:
                business.category = categories[0]
                business.save(update_fields=["category"])
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

    def get_queryset(self):
        qs = (
            Order.objects.filter(business=self.request.user.business_profile)
            .select_related("business", "branch", "user")
            .prefetch_related("items", "payment_proofs", "delivery_snapshot")
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs


class BusinessOrderDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]
    serializer_class = OrderSerializer
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"

    def get_queryset(self):
        return Order.objects.filter(business=self.request.user.business_profile)


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
            order.save(update_fields=["status", "updated_at"])
        elif proof.review_status == OrderPaymentProof.ReviewStatus.REJECTED:
            order.status = Order.Status.AWAITING_PAYMENT
            order.save(update_fields=["status", "updated_at"])
        return Response(
            OrderPaymentProofSerializer(proof, context={"request": request}).data
        )


class BusinessStatsAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        business = request.user.business_profile
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
            }
        )


# ---- Admin category tree ----


class AdminCategoryTreeListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def get(self, request):
        roots = Category.objects.filter(parent__isnull=True).order_by(
            "sort_order", "name", "id"
        )
        return Response(CategoryTreeSerializer(roots, many=True).data)

    def post(self, request):
        serializer = CategoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return Response(
            CategoryWriteSerializer(category).data, status=status.HTTP_201_CREATED
        )


class AdminCategoryTreeDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def patch(self, request, category_id: int):
        category = get_object_or_404(Category, pk=category_id)
        serializer = CategoryWriteSerializer(
            category, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, category_id: int):
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
        return Order.objects.select_related("business", "branch", "user").all()


class AdminProductListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]
    serializer_class = ProductSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        return Product.objects.select_related("business", "category").all()


class EnsureGeoSeedAPIView(APIView):
    """Dev helper — ensure default country exists."""

    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request):
        country = get_or_create_default_country()
        return Response(CountrySerializer(country).data)
