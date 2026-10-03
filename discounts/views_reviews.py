"""Product review API views (consumer, business, admin, public)."""

from __future__ import annotations

from django.db.models import Case, IntegerField, When
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Branch, Business, Order, OrderItem, Product, ProductReview
from .pagination import page_payload, parse_page_params, slice_queryset
from .permissions import IsAdminAccount, IsBusinessAccount, IsConsumerAccount
from .review_service import (
    admin_dismiss_flag,
    admin_hide_review,
    admin_restore_review,
    create_review,
    merchant_flag,
    merchant_reply,
    parse_optional_branch_id,
    public_reviews_qs,
    update_review,
)
from .serializers_commerce import (
    MerchantReviewFlagSerializer,
    MerchantReviewReplySerializer,
    ProductReviewCreateSerializer,
    ProductReviewSerializer,
    ProductReviewUpdateSerializer,
)


def _review_queryset():
    return ProductReview.objects.select_related(
        "user", "product", "business", "branch", "order", "order_item"
    ).prefetch_related("images")


def _apply_review_sort(qs, sort: str):
    if sort == "highest":
        return qs.order_by("-rating", "-created_at", "-id")
    if sort == "lowest":
        return qs.order_by("rating", "-created_at", "-id")
    return qs.order_by("-created_at", "-id")


class ConsumerOrderItemReviewCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, public_id, item_id: int):
        order = get_object_or_404(Order, public_id=public_id, user=request.user)
        order_item = get_object_or_404(OrderItem, pk=item_id, order=order)
        serializer = ProductReviewCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        images = serializer.validated_data.get("images") or request.FILES.getlist(
            "images"
        )
        review = create_review(
            user=request.user,
            order=order,
            order_item=order_item,
            rating=serializer.validated_data["rating"],
            comment=serializer.validated_data.get("comment") or "",
            images=list(images) if images else None,
        )
        from .notification_utils import notify_business_new_review

        notify_business_new_review(review)
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ConsumerReviewUpdateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, review_id: int):
        review = get_object_or_404(_review_queryset(), pk=review_id, user=request.user)
        serializer = ProductReviewUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        images = serializer.validated_data.get("images")
        if images is None and request.FILES:
            images = request.FILES.getlist("images") or None
        review = update_review(
            user=request.user,
            review=review,
            rating=serializer.validated_data.get("rating"),
            comment=serializer.validated_data.get("comment"),
            images=list(images) if images is not None else None,
            replace_images=bool(serializer.validated_data.get("replace_images")),
        )
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )


class ProductReviewListAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, product_id: int):
        get_object_or_404(Product, pk=product_id)
        page, page_size = parse_page_params(request)
        sort = (request.query_params.get("sort") or "newest").strip().lower()
        qs = public_reviews_qs().filter(product_id=product_id)
        branch_id = parse_optional_branch_id(request.query_params.get("branch_id"))
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        qs = _apply_review_sort(qs, sort)
        count, items = slice_queryset(qs, page, page_size)
        return Response(
            page_payload(
                count=count,
                page=page,
                page_size=page_size,
                results=ProductReviewSerializer(
                    items, many=True, context={"request": request}
                ).data,
            )
        )


class BusinessReviewListPublicAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, business_id: int):
        get_object_or_404(Business, pk=business_id)
        page, page_size = parse_page_params(request)
        sort = (request.query_params.get("sort") or "newest").strip().lower()
        qs = public_reviews_qs().filter(business_id=business_id)
        branch_id = parse_optional_branch_id(request.query_params.get("branch_id"))
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        qs = _apply_review_sort(qs, sort)
        count, items = slice_queryset(qs, page, page_size)
        return Response(
            page_payload(
                count=count,
                page=page,
                page_size=page_size,
                results=ProductReviewSerializer(
                    items, many=True, context={"request": request}
                ).data,
            )
        )


class BranchReviewListPublicAPIView(APIView):
    """Public reviews for a single store location."""

    permission_classes = [AllowAny]

    def get(self, request, branch_id: int):
        get_object_or_404(Branch, pk=branch_id)
        page, page_size = parse_page_params(request)
        sort = (request.query_params.get("sort") or "newest").strip().lower()
        qs = _apply_review_sort(
            public_reviews_qs().filter(branch_id=branch_id), sort
        )
        count, items = slice_queryset(qs, page, page_size)
        return Response(
            page_payload(
                count=count,
                page=page,
                page_size=page_size,
                results=ProductReviewSerializer(
                    items, many=True, context={"request": request}
                ).data,
            )
        )


class BusinessReviewListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def get(self, request):
        business = request.user.business_profile
        page, page_size = parse_page_params(request)
        qs = _review_queryset().filter(business=business)
        status_filter = (request.query_params.get("status") or "").strip().lower()
        if status_filter in ProductReview.Status.values:
            qs = qs.filter(status=status_filter)
        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)
        branch_id = parse_optional_branch_id(request.query_params.get("branch_id"))
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        rating = request.query_params.get("rating")
        if rating:
            try:
                qs = qs.filter(rating=int(rating))
            except (TypeError, ValueError):
                pass
        flagged_only = (request.query_params.get("flagged") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        if flagged_only:
            qs = qs.filter(status=ProductReview.Status.FLAGGED)
        qs = qs.annotate(
            _flag_priority=Case(
                When(status=ProductReview.Status.FLAGGED, then=0),
                default=1,
                output_field=IntegerField(),
            )
        ).order_by("_flag_priority", "-created_at", "-id")
        count, items = slice_queryset(qs, page, page_size)
        return Response(
            page_payload(
                count=count,
                page=page,
                page_size=page_size,
                results=ProductReviewSerializer(
                    items, many=True, context={"request": request}
                ).data,
            )
        )


class BusinessReviewReplyAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, review_id: int):
        business = request.user.business_profile
        review = get_object_or_404(_review_queryset(), pk=review_id, business=business)
        serializer = MerchantReviewReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review = merchant_reply(
            business=business, review=review, reply=serializer.validated_data["reply"]
        )
        from .notification_utils import notify_customer_review_reply

        notify_customer_review_reply(review)
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )


class BusinessReviewFlagAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessAccount]

    def post(self, request, review_id: int):
        business = request.user.business_profile
        review = get_object_or_404(_review_queryset(), pk=review_id, business=business)
        serializer = MerchantReviewFlagSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review = merchant_flag(
            business=business,
            review=review,
            reason=serializer.validated_data.get("reason") or "",
        )
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )


class AdminReviewListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def get(self, request):
        page, page_size = parse_page_params(request)
        qs = _review_queryset()
        status_filter = (request.query_params.get("status") or "").strip().lower()
        if status_filter in ProductReview.Status.values:
            qs = qs.filter(status=status_filter)
        business_id = request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        branch_id = parse_optional_branch_id(request.query_params.get("branch_id"))
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)
        rating = request.query_params.get("rating")
        if rating:
            try:
                qs = qs.filter(rating=int(rating))
            except (TypeError, ValueError):
                pass
        qs = qs.annotate(
            _flag_priority=Case(
                When(status=ProductReview.Status.FLAGGED, then=0),
                default=1,
                output_field=IntegerField(),
            )
        ).order_by("_flag_priority", "-created_at", "-id")
        count, items = slice_queryset(qs, page, page_size)
        return Response(
            page_payload(
                count=count,
                page=page,
                page_size=page_size,
                results=ProductReviewSerializer(
                    items, many=True, context={"request": request}
                ).data,
            )
        )


class AdminReviewHideAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, review_id: int):
        review = get_object_or_404(_review_queryset(), pk=review_id)
        review = admin_hide_review(review)
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )


class AdminReviewRestoreAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, review_id: int):
        review = get_object_or_404(_review_queryset(), pk=review_id)
        review = admin_restore_review(review)
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )


class AdminReviewDismissFlagAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminAccount]

    def post(self, request, review_id: int):
        review = get_object_or_404(_review_queryset(), pk=review_id)
        review = admin_dismiss_flag(review)
        return Response(
            ProductReviewSerializer(review, context={"request": request}).data
        )
