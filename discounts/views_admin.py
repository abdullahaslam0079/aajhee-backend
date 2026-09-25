from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum, TextField
from django.db.models.functions import Cast, TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.views import TokenObtainPairView

from .auth_utils import blacklist_user_tokens, logout_response_message
from .delivery_options import get_or_create_fulfillment_settings
from .models import (
    Branch,
    BranchContact,
    Business,
    BusinessEngagementStats,
    Category,
    DealSource,
    Offer,
    OfferBranchStats,
    OfferEngagementStats,
    OfferRedemption,
    OfferScan,
    OfferViewEvent,
)
from .notification_utils import notify_favorited_business_new_offer
from .offer_sync import sync_deal_source
from .offer_utils import branch_highlight_queryset
from .permissions import IsAdminAccount
from .serializers_admin import (
    AdminBranchSerializer,
    AdminBusinessCreateSerializer,
    AdminBusinessSerializer,
    AdminCategorySerializer,
    AdminDealSourceSerializer,
    AdminLoginTokenObtainPairSerializer,
    AdminOfferSerializer,
    AdminProfileSerializer,
    AdminUserSerializer,
    AdminUserUpdateSerializer,
)
from .serializers_commerce import (
    BranchContactSerializer,
    BranchFulfillmentSettingsSerializer,
)

User = get_user_model()


def _paginate(queryset, request, serializer_class, context=None):
    from .pagination import page_payload, parse_page_params, slice_queryset

    page, page_size = parse_page_params(request)
    total, items = slice_queryset(queryset, page, page_size)
    serializer = serializer_class(items, many=True, context=context or {"request": request})
    return Response(
        page_payload(
            count=total,
            page=page,
            page_size=page_size,
            results=serializer.data,
        )
    )


def _business_queryset():
    return (
        Business.objects.select_related("owner", "category", "engagement_stats")
        .annotate(
            annotated_branch_count=Count("branches", distinct=True),
            annotated_offer_count=Count("offers", distinct=True),
            annotated_scan_count=Sum("offers__branch_stats__scan_count"),
            annotated_redemption_count=Count("offers__redemptions", distinct=True),
        )
        .order_by("name", "id")
    )


class AdminLoginAPIView(TokenObtainPairView):
    authentication_classes = []
    serializer_class = AdminLoginTokenObtainPairSerializer


class AdminLogoutAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request):
        refresh = request.data.get("refresh")
        try:
            blacklist_user_tokens(request.user, refresh=refresh or None)
        except TokenError:
            return Response(
                {
                    "message": "Invalid or expired token.",
                    "errors": {"refresh": ["Invalid or expired token."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "message": logout_response_message(refresh),
                "errors": {},
            },
            status=status.HTTP_200_OK,
        )


class AdminMeAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request):
        return Response(AdminProfileSerializer(request.user).data)


class AdminAnalyticsOverviewAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request):
        now = timezone.now()
        active_offers = Offer.objects.filter(is_enabled=True).filter(
            Q(is_time_limited=False)
            | (
                Q(is_time_limited=True)
                & (Q(starts_at__isnull=True) | Q(starts_at__lte=now))
                & (Q(ends_at__isnull=True) | Q(ends_at__gte=now))
            )
        )

        scan_total = OfferBranchStats.objects.aggregate(total=Sum("scan_count"))[
            "total"
        ] or 0
        avail_total = OfferBranchStats.objects.aggregate(total=Sum("avail_count"))[
            "total"
        ] or 0
        offer_views = OfferEngagementStats.objects.aggregate(total=Sum("view_count"))[
            "total"
        ] or 0
        offer_likes = OfferEngagementStats.objects.aggregate(total=Sum("like_count"))[
            "total"
        ] or 0
        business_views = BusinessEngagementStats.objects.aggregate(
            total=Sum("view_count")
        )["total"] or 0
        business_likes = BusinessEngagementStats.objects.aggregate(
            total=Sum("like_count")
        )["total"] or 0

        top_businesses = list(
            Business.objects.annotate(
                scan_count=Sum("offers__branch_stats__scan_count"),
                redemption_count=Count("offers__redemptions", distinct=True),
            )
            .order_by("-scan_count", "-redemption_count", "name")[:5]
            .values("id", "name", "scan_count", "redemption_count")
        )
        for item in top_businesses:
            item["scan_count"] = int(item["scan_count"] or 0)
            item["redemption_count"] = int(item["redemption_count"] or 0)

        recent_businesses = AdminBusinessSerializer(
            _business_queryset().order_by("-id")[:5],
            many=True,
            context={"request": request},
        ).data
        recent_offers = AdminOfferSerializer(
            Offer.objects.select_related(
                "business", "business__category", "engagement_stats"
            )
            .prefetch_related("branches", "branch_stats__branch", "gallery_images")
            .annotate(
                annotated_unique_viewers=Count(
                    "view_events__user",
                    distinct=True,
                    filter=Q(view_events__user__isnull=False),
                )
            )
            .order_by("-created_at", "-id")[:5],
            many=True,
            context={"request": request},
        ).data

        return Response(
            {
                "counts": {
                    "consumers": User.objects.filter(
                        account_type=User.AccountType.CONSUMER
                    ).count(),
                    "businesses": Business.objects.count(),
                    "branches": Branch.objects.count(),
                    "offers_total": Offer.objects.count(),
                    "offers_active": active_offers.count(),
                    "scans": int(scan_total),
                    "avails": int(avail_total),
                    "redemptions": OfferRedemption.objects.count(),
                    "offer_views": int(offer_views),
                    "offer_likes": int(offer_likes),
                    "business_views": int(business_views),
                    "business_likes": int(business_likes),
                    "users_total": User.objects.count(),
                },
                "top_businesses": top_businesses,
                "recent_businesses": recent_businesses,
                "recent_offers": recent_offers,
            }
        )


class AdminAnalyticsTimeseriesAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request):
        try:
            days = min(max(int(request.query_params.get("days", 30)), 1), 90)
        except (TypeError, ValueError):
            days = 30

        today = timezone.localdate()
        start = today - timedelta(days=days - 1)

        scans_by_day = {
            row["day"]: row["count"]
            for row in OfferScan.objects.filter(scanned_at__date__gte=start)
            .annotate(day=TruncDate("scanned_at"))
            .values("day")
            .annotate(count=Count("id"))
        }
        redemptions_by_day = {
            row["day"]: row["count"]
            for row in OfferRedemption.objects.filter(redeemed_at__date__gte=start)
            .annotate(day=TruncDate("redeemed_at"))
            .values("day")
            .annotate(count=Count("id"))
        }
        views_by_day = {
            row["viewed_on"]: row["count"]
            for row in OfferViewEvent.objects.filter(viewed_on__gte=start)
            .values("viewed_on")
            .annotate(count=Count("id"))
        }

        series = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            series.append(
                {
                    "date": day.isoformat(),
                    "scans": int(scans_by_day.get(day, 0)),
                    "redemptions": int(redemptions_by_day.get(day, 0)),
                    "views": int(views_by_day.get(day, 0)),
                }
            )

        return Response({"days": days, "series": series})


class AdminBusinessListCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        qs = _business_queryset()
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(owner__email__icontains=search)
                | Q(category__name__icontains=search)
            )
        category_id = request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(category_id=category_id)
        return _paginate(qs, request, AdminBusinessSerializer)

    def post(self, request):
        serializer = AdminBusinessCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        business = serializer.save()
        output = AdminBusinessSerializer(
            _business_queryset().get(pk=business.pk),
            context={"request": request},
        ).data
        return Response(
            {
                "message": "Business created successfully.",
                "errors": {},
                **output,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminBusinessDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_object(self, business_id: int) -> Business:
        return get_object_or_404(_business_queryset(), pk=business_id)

    def get(self, request, business_id: int):
        business = self.get_object(business_id)
        return Response(
            AdminBusinessSerializer(business, context={"request": request}).data
        )

    def put(self, request, business_id: int):
        return self._update(request, business_id, partial=False)

    def patch(self, request, business_id: int):
        return self._update(request, business_id, partial=True)

    def _update(self, request, business_id: int, partial: bool):
        business = self.get_object(business_id)
        serializer = AdminBusinessSerializer(
            business,
            data=request.data,
            partial=partial,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        refreshed = self.get_object(business_id)
        return Response(
            {
                "message": "Business updated successfully.",
                "errors": {},
                **AdminBusinessSerializer(
                    refreshed, context={"request": request}
                ).data,
            }
        )

    def delete(self, request, business_id: int):
        business = get_object_or_404(Business.objects.select_related("owner"), pk=business_id)
        owner = business.owner
        business.delete()
        if owner.account_type == User.AccountType.BUSINESS:
            owner.delete()
        return Response(
            {"message": "Business deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )


class AdminBusinessBranchListCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_business(self, business_id: int) -> Business:
        return get_object_or_404(Business, pk=business_id)

    def get(self, request, business_id: int):
        business = self.get_business(business_id)
        qs = branch_highlight_queryset(
            business.branches.all(),
            timezone.now(),
        ).order_by("name", "id")
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(city__icontains=search)
                | Q(street__icontains=search)
            )
        return _paginate(
            qs,
            request,
            AdminBranchSerializer,
            context={"request": request, "business": business},
        )

    def post(self, request, business_id: int):
        business = self.get_business(business_id)
        serializer = AdminBranchSerializer(
            data=request.data,
            context={"request": request, "business": business},
        )
        serializer.is_valid(raise_exception=True)
        branch = serializer.save()
        output = AdminBranchSerializer(
            branch, context={"request": request, "business": business}
        ).data
        return Response(
            {
                "message": "Branch created successfully.",
                "errors": {},
                **output,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminBranchDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_object(self, branch_id: int) -> Branch:
        return get_object_or_404(
            branch_highlight_queryset(Branch.objects.select_related("business"), timezone.now()),
            pk=branch_id,
        )

    def get(self, request, branch_id: int):
        branch = self.get_object(branch_id)
        return Response(
            AdminBranchSerializer(
                branch,
                context={"request": request, "business": branch.business},
            ).data
        )

    def put(self, request, branch_id: int):
        return self._update(request, branch_id, partial=False)

    def patch(self, request, branch_id: int):
        return self._update(request, branch_id, partial=True)

    def _update(self, request, branch_id: int, partial: bool):
        branch = self.get_object(branch_id)
        serializer = AdminBranchSerializer(
            branch,
            data=request.data,
            partial=partial,
            context={"request": request, "business": branch.business},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "message": "Branch updated successfully.",
                "errors": {},
                **AdminBranchSerializer(
                    branch,
                    context={"request": request, "business": branch.business},
                ).data,
            }
        )

    def delete(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        if branch.offers.exists():
            return Response(
                {
                    "message": "Cannot delete a branch that has offers assigned to it.",
                    "errors": {"branch_id": ["Remove offers from this branch first."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        branch.delete()
        return Response(
            {"message": "Branch deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )


class AdminBranchContactsAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        return Response(
            BranchContactSerializer(branch.contacts.all(), many=True).data
        )

    def put(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        serializer = BranchContactSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        branch.contacts.all().delete()
        created = [
            BranchContact.objects.create(branch=branch, **item)
            for item in serializer.validated_data
        ]
        return Response(BranchContactSerializer(created, many=True).data)


class AdminBranchFulfillmentAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        settings = get_or_create_fulfillment_settings(branch)
        return Response(BranchFulfillmentSettingsSerializer(settings).data)

    def patch(self, request, branch_id: int):
        branch = get_object_or_404(Branch, pk=branch_id)
        settings = get_or_create_fulfillment_settings(branch)
        serializer = BranchFulfillmentSettingsSerializer(
            settings, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema_view(
    get=extend_schema(
        summary="List offers (admin)",
        description=(
            "Paginated offers. Each item includes `view_count` (total opens) and "
            "`unique_viewers` (distinct authenticated users who opened the offer)."
        ),
        responses={200: AdminOfferSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Create offer (admin)",
        request=AdminOfferSerializer,
        responses={201: AdminOfferSerializer},
    ),
)
class AdminOfferListCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return (
            Offer.objects.select_related(
                "business", "business__category", "engagement_stats"
            )
            .prefetch_related("branches", "branch_stats__branch", "gallery_images")
            .annotate(
                annotated_unique_viewers=Count(
                    "view_events__user",
                    distinct=True,
                    filter=Q(view_events__user__isnull=False),
                )
            )
            .order_by("-created_at", "-id")
        )

    def get(self, request):
        qs = self.get_queryset()
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.annotate(
                included_items_text=Cast("included_items", TextField())
            ).filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(item_name__icontains=search)
                | Q(included_items_text__icontains=search)
                | Q(business__name__icontains=search)
            )
        business_id = request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        is_enabled = request.query_params.get("is_enabled")
        if is_enabled is not None:
            if is_enabled.lower() in ("1", "true", "yes"):
                qs = qs.filter(is_enabled=True)
            elif is_enabled.lower() in ("0", "false", "no"):
                qs = qs.filter(is_enabled=False)
        review_status = (request.query_params.get("review_status") or "").strip()
        if review_status in {
            Offer.ReviewStatus.PENDING,
            Offer.ReviewStatus.APPROVED,
            Offer.ReviewStatus.REJECTED,
        }:
            qs = qs.filter(review_status=review_status)
        origin = (request.query_params.get("origin") or "").strip()
        if origin in {
            Offer.Origin.MANUAL,
            Offer.Origin.BRAND_LISTING,
            Offer.Origin.AFFILIATE_FEED,
        }:
            qs = qs.filter(origin=origin)
        return _paginate(qs, request, AdminOfferSerializer)

    def post(self, request):
        business_id = request.data.get("business_id")
        business = None
        if business_id:
            business = get_object_or_404(Business, pk=business_id)
        serializer = AdminOfferSerializer(
            data=request.data,
            context={"request": request, "business": business},
        )
        serializer.is_valid(raise_exception=True)
        offer = serializer.save()
        notify_favorited_business_new_offer(offer)
        offer = self.get_queryset().get(pk=offer.pk)
        output = AdminOfferSerializer(
            offer, context={"request": request, "business": offer.business}
        ).data
        return Response(
            {
                "message": "Offer created successfully.",
                "errors": {},
                **output,
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    get=extend_schema(
        summary="Get offer (admin)",
        description=(
            "Offer detail including engagement: `view_count` (total opens) and "
            "`unique_viewers` (distinct authenticated users)."
        ),
        responses={200: AdminOfferSerializer},
    ),
    put=extend_schema(
        summary="Replace offer (admin)",
        request=AdminOfferSerializer,
        responses={200: AdminOfferSerializer},
    ),
    patch=extend_schema(
        summary="Update offer (admin)",
        request=AdminOfferSerializer,
        responses={200: AdminOfferSerializer},
    ),
    delete=extend_schema(summary="Delete offer (admin)"),
)
class AdminOfferDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return (
            Offer.objects.select_related(
                "business", "business__category", "engagement_stats"
            )
            .prefetch_related("branches", "branch_stats__branch", "gallery_images")
            .annotate(
                annotated_unique_viewers=Count(
                    "view_events__user",
                    distinct=True,
                    filter=Q(view_events__user__isnull=False),
                )
            )
        )

    def get_object(self, offer_id: int) -> Offer:
        return get_object_or_404(self.get_queryset(), pk=offer_id)

    def get(self, request, offer_id: int):
        offer = self.get_object(offer_id)
        return Response(
            AdminOfferSerializer(
                offer, context={"request": request, "business": offer.business}
            ).data
        )

    def put(self, request, offer_id: int):
        return self._update(request, offer_id, partial=False)

    def patch(self, request, offer_id: int):
        return self._update(request, offer_id, partial=True)

    def _update(self, request, offer_id: int, partial: bool):
        offer = self.get_object(offer_id)
        serializer = AdminOfferSerializer(
            offer,
            data=request.data,
            partial=partial,
            context={"request": request, "business": offer.business},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        offer = self.get_object(offer_id)
        return Response(
            {
                "message": "Offer updated successfully.",
                "errors": {},
                **AdminOfferSerializer(
                    offer, context={"request": request, "business": offer.business}
                ).data,
            }
        )

    def delete(self, request, offer_id: int):
        offer = get_object_or_404(Offer, pk=offer_id)
        offer.delete()
        return Response(
            {"message": "Offer deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )


def _admin_offer_payload(offer: Offer, request) -> dict:
    return AdminOfferSerializer(
        offer, context={"request": request, "business": offer.business}
    ).data


class AdminOfferApproveAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request, offer_id: int):
        offer = get_object_or_404(Offer, pk=offer_id)
        if offer.review_status == Offer.ReviewStatus.REJECTED:
            return Response(
                {
                    "message": "Rejected offers cannot be approved. Create a new import instead.",
                    "errors": {"review_status": ["This offer was rejected."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        was_pending = offer.review_status == Offer.ReviewStatus.PENDING
        offer.review_status = Offer.ReviewStatus.APPROVED
        offer.is_enabled = True
        offer.disabled_by = ""
        offer.unavailable_reason = ""
        offer.save(
            update_fields=[
                "review_status",
                "is_enabled",
                "disabled_by",
                "unavailable_reason",
            ]
        )
        if was_pending:
            notify_favorited_business_new_offer(offer)
        return Response(
            {
                "message": "Offer approved.",
                "errors": {},
                **_admin_offer_payload(offer, request),
            }
        )


class AdminOfferRejectAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request, offer_id: int):
        offer = get_object_or_404(Offer, pk=offer_id)
        if offer.origin == Offer.Origin.MANUAL:
            return Response(
                {
                    "message": "Manual offers cannot be rejected from the review queue.",
                    "errors": {"origin": ["Only imported offers can be rejected."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        offer.review_status = Offer.ReviewStatus.REJECTED
        offer.is_enabled = False
        offer.disabled_by = Offer.DisabledBy.ADMIN
        offer.save(update_fields=["review_status", "is_enabled", "disabled_by"])
        return Response(
            {
                "message": "Offer rejected.",
                "errors": {},
                **_admin_offer_payload(offer, request),
            }
        )


class AdminOfferBulkApproveAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request):
        raw_ids = request.data.get("ids") or request.data.get("offer_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {
                    "message": "Provide a list of offer ids.",
                    "errors": {"ids": ["This field is required."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        ids = []
        for item in raw_ids:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        offers = list(
            Offer.objects.filter(pk__in=ids).exclude(
                review_status=Offer.ReviewStatus.REJECTED
            )
        )
        approved = 0
        for offer in offers:
            was_pending = offer.review_status == Offer.ReviewStatus.PENDING
            offer.review_status = Offer.ReviewStatus.APPROVED
            offer.is_enabled = True
            offer.disabled_by = ""
            offer.unavailable_reason = ""
            offer.save(
                update_fields=[
                    "review_status",
                    "is_enabled",
                    "disabled_by",
                    "unavailable_reason",
                ]
            )
            if was_pending:
                notify_favorited_business_new_offer(offer)
            approved += 1
        return Response(
            {
                "message": f"Approved {approved} offer(s).",
                "errors": {},
                "approved": approved,
            }
        )


class AdminBusinessDealSourceListCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_business(self, business_id: int) -> Business:
        return get_object_or_404(Business, pk=business_id)

    def get(self, request, business_id: int):
        business = self.get_business(business_id)
        qs = business.deal_sources.all().order_by("name", "id")
        serializer = AdminDealSourceSerializer(
            qs, many=True, context={"request": request, "business": business}
        )
        return Response(serializer.data)

    def post(self, request, business_id: int):
        business = self.get_business(business_id)
        serializer = AdminDealSourceSerializer(
            data=request.data, context={"request": request, "business": business}
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        return Response(
            {
                "message": "Deal source created.",
                "errors": {},
                **AdminDealSourceSerializer(
                    source, context={"request": request, "business": business}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminDealSourceDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_object(self, source_id: int) -> DealSource:
        return get_object_or_404(DealSource.objects.select_related("business"), pk=source_id)

    def get(self, request, source_id: int):
        source = self.get_object(source_id)
        return Response(
            AdminDealSourceSerializer(
                source, context={"request": request, "business": source.business}
            ).data
        )

    def patch(self, request, source_id: int):
        return self._update(request, source_id, partial=True)

    def put(self, request, source_id: int):
        return self._update(request, source_id, partial=False)

    def _update(self, request, source_id: int, partial: bool):
        source = self.get_object(source_id)
        serializer = AdminDealSourceSerializer(
            source,
            data=request.data,
            partial=partial,
            context={"request": request, "business": source.business},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "message": "Deal source updated.",
                "errors": {},
                **AdminDealSourceSerializer(
                    source, context={"request": request, "business": source.business}
                ).data,
            }
        )

    def delete(self, request, source_id: int):
        source = self.get_object(source_id)
        source.delete()
        return Response(
            {"message": "Deal source deleted.", "errors": {}},
            status=status.HTTP_200_OK,
        )


class AdminDealSourceSyncAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request, source_id: int):
        source = get_object_or_404(
            DealSource.objects.select_related("business"), pk=source_id
        )
        result = sync_deal_source(source)
        source.refresh_from_db()
        return Response(
            {
                "message": "Deal source synced.",
                "errors": {},
                "result": result.as_dict(),
                "source": AdminDealSourceSerializer(
                    source, context={"request": request, "business": source.business}
                ).data,
            }
        )


class AdminUserListAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request):
        qs = (
            User.objects.select_related("business_profile")
            .order_by("-date_joined", "-id")
        )
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        account_type = (request.query_params.get("account_type") or "").strip()
        if account_type in {User.AccountType.CONSUMER, User.AccountType.BUSINESS}:
            qs = qs.filter(account_type=account_type)
        is_active = request.query_params.get("is_active")
        if is_active is not None:
            if is_active.lower() in ("1", "true", "yes"):
                qs = qs.filter(is_active=True)
            elif is_active.lower() in ("0", "false", "no"):
                qs = qs.filter(is_active=False)
        # Hide staff from the default users list unless explicitly requested.
        include_staff = (request.query_params.get("include_staff") or "").lower()
        if include_staff not in ("1", "true", "yes"):
            qs = qs.filter(is_staff=False)
        return _paginate(qs, request, AdminUserSerializer)


class AdminUserDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_object(self, user_id: int) -> User:
        return get_object_or_404(
            User.objects.select_related("business_profile"), pk=user_id
        )

    def get(self, request, user_id: int):
        user = self.get_object(user_id)
        return Response(AdminUserSerializer(user).data)

    def patch(self, request, user_id: int):
        user = self.get_object(user_id)
        if user.is_superuser and request.user.pk == user.pk:
            if request.data.get("is_active") is False:
                return Response(
                    {
                        "message": "You cannot deactivate your own admin account.",
                        "errors": {"is_active": ["Cannot deactivate yourself."]},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        serializer = AdminUserUpdateSerializer(
            user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "message": "User updated successfully.",
                "errors": {},
                **AdminUserSerializer(user).data,
            }
        )


class AdminCategoryListCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_queryset(self):
        return Category.objects.annotate(
            business_count=Count("businesses")
        ).order_by("name", "id")

    def get(self, request):
        qs = self.get_queryset()
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        return _paginate(qs, request, AdminCategorySerializer)

    def post(self, request):
        serializer = AdminCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        category = self.get_queryset().get(pk=category.pk)
        return Response(
            {
                "message": "Category created successfully.",
                "errors": {},
                **AdminCategorySerializer(category).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminCategoryDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_queryset(self):
        return Category.objects.annotate(business_count=Count("businesses"))

    def get_object(self, category_id: int) -> Category:
        return get_object_or_404(self.get_queryset(), pk=category_id)

    def get(self, request, category_id: int):
        return Response(AdminCategorySerializer(self.get_object(category_id)).data)

    def put(self, request, category_id: int):
        return self._update(request, category_id, partial=False)

    def patch(self, request, category_id: int):
        return self._update(request, category_id, partial=True)

    def _update(self, request, category_id: int, partial: bool):
        category = self.get_object(category_id)
        serializer = AdminCategorySerializer(
            category, data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        category = self.get_object(category_id)
        return Response(
            {
                "message": "Category updated successfully.",
                "errors": {},
                **AdminCategorySerializer(category).data,
            }
        )

    def delete(self, request, category_id: int):
        category = get_object_or_404(Category, pk=category_id)
        in_use = (
            category.businesses.exists()
            or category.primary_businesses.exists()
            or category.products.exists()
        )
        if in_use:
            return Response(
                {
                    "message": "Cannot delete a category that has businesses or products.",
                    "errors": {
                        "category_id": ["Reassign or remove businesses/products first."]
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        category.delete()
        return Response(
            {"message": "Category deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )
