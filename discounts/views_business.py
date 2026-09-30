from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.views import TokenObtainPairView

from .auth_utils import blacklist_user_tokens, logout_response_message
from .models import Branch, Offer, OfferRedemption, OfferScan
from .notification_utils import notify_favorited_business_new_offer
from .offer_utils import (
    branch_highlight_queryset,
    get_user_offer_usage_status,
    increment_branch_stat,
    payment_record_fields,
    record_offer_redemption,
)
from .permissions import IsBusinessAccount, IsConsumerAccount
from .serializers import OfferPaymentPreviewSerializer, OfferUsageSerializer
from .serializers_business import (
    BranchSerializer,
    BusinessLoginTokenObtainPairSerializer,
    BusinessOfferSerializer,
    BusinessProfileSerializer,
    BusinessRegisterSerializer,
    OfferRedeemSerializer,
    OfferScanSerializer,
)
from .views_auth import AuthScopedThrottleMixin


class BusinessRegisterAPIView(AuthScopedThrottleMixin, generics.CreateAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    serializer_class = BusinessRegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        business = serializer.save()
        return Response(
            {
                "message": "Business account created successfully.",
                "errors": {},
                "business": BusinessProfileSerializer(
                    business, context={"request": request}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


class BusinessLoginAPIView(AuthScopedThrottleMixin, TokenObtainPairView):
    authentication_classes = []
    serializer_class = BusinessLoginTokenObtainPairSerializer


class BusinessLogoutAPIView(APIView):
    permission_classes = [IsBusinessAccount]

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


class BusinessProfileAPIView(APIView):
    permission_classes = [IsBusinessAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        business = request.user.business_profile
        serializer = BusinessProfileSerializer(
            business, context={"request": request}
        )
        return Response(serializer.data)

    def put(self, request):
        business = request.user.business_profile
        serializer = BusinessProfileSerializer(
            business,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BusinessBranchListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = BranchSerializer
    permission_classes = [IsBusinessAccount]

    def get_queryset(self):
        return branch_highlight_queryset(
            self.request.user.business_profile.branches.all(),
            timezone.now(),
        ).order_by("name", "id")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.request.user.business_profile
        return context

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        branch = serializer.save()
        output = BranchSerializer(branch, context=self.get_serializer_context()).data
        return Response(
            {
                "message": "Branch created successfully.",
                "errors": {},
                **output,
            },
            status=status.HTTP_201_CREATED,
        )


class BusinessBranchDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BranchSerializer
    permission_classes = [IsBusinessAccount]
    lookup_url_kwarg = "branch_id"

    def get_queryset(self):
        return branch_highlight_queryset(
            self.request.user.business_profile.branches.all(),
            timezone.now(),
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.request.user.business_profile
        return context

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        response.data = {
            "message": "Branch updated successfully.",
            "errors": {},
            **response.data,
        }
        return response

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.offers.exists():
            return Response(
                {
                    "message": "Cannot delete a branch that has offers assigned to it.",
                    "errors": {"branch_id": ["Remove offers from this branch first."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        instance.delete()
        return Response(
            {"message": "Branch deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )


class BusinessOfferListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = BusinessOfferSerializer
    permission_classes = [IsBusinessAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return (
            self.request.user.business_profile.offers.select_related(
                "engagement_stats"
            )
            .prefetch_related("branches", "branch_stats__branch", "gallery_images")
            .order_by("-created_at", "-id")
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.request.user.business_profile
        return context

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offer = serializer.save()
        notify_favorited_business_new_offer(offer)
        output = BusinessOfferSerializer(
            offer, context=self.get_serializer_context()
        ).data
        return Response(
            {
                "message": "Offer created successfully.",
                "errors": {},
                **output,
            },
            status=status.HTTP_201_CREATED,
        )


class BusinessOfferDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BusinessOfferSerializer
    permission_classes = [IsBusinessAccount]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_url_kwarg = "offer_id"

    def get_queryset(self):
        return self.request.user.business_profile.offers.select_related(
            "engagement_stats"
        ).prefetch_related(
            "branches", "branch_stats__branch", "gallery_images"
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.request.user.business_profile
        return context

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        response.data = {
            "message": "Offer updated successfully.",
            "errors": {},
            **response.data,
        }
        return response

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.delete()
        return Response(
            {"message": "Offer deleted successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )


class OfferScanAPIView(APIView):
    permission_classes = [IsConsumerAccount]

    def post(self, request, offer_id):
        offer = get_object_or_404(Offer, pk=offer_id)
        serializer = OfferScanSerializer(
            data=request.data,
            context={"offer": offer, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        branch = serializer.validated_data["branch"]
        payment = serializer.validated_data["payment"]

        with transaction.atomic():
            OfferScan.objects.create(
                offer=offer,
                branch=branch,
                user=request.user,
                **payment_record_fields(payment),
            )
            increment_branch_stat(offer, branch, scan=True)

        payment_data = OfferPaymentPreviewSerializer.from_preview(payment).data
        return Response(
            {
                "message": "Offer scanned successfully.",
                "errors": {},
                "offer_id": offer.id,
                "branch_id": branch.id,
                "payment": payment_data,
            },
            status=status.HTTP_200_OK,
        )


class OfferRedeemAPIView(APIView):
    permission_classes = [IsConsumerAccount]

    def post(self, request, offer_id):
        offer = get_object_or_404(Offer, pk=offer_id)
        serializer = OfferRedeemSerializer(
            data=request.data,
            context={"offer": offer, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        branch = serializer.validated_data["branch"]
        payment = serializer.validated_data["payment"]

        with transaction.atomic():
            locked_offer = Offer.objects.select_for_update().get(pk=offer.pk)
            usage, error = record_offer_redemption(
                request.user, locked_offer, branch, payment, record_scan=False
            )
            if usage is None:
                return Response(
                    {
                        "message": error,
                        "errors": {"non_field_errors": [error]},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            usage_data = OfferUsageSerializer.from_usage(locked_offer.id, usage).data

        payment_data = OfferPaymentPreviewSerializer.from_preview(payment).data
        return Response(
            {
                "errors": {},
                "offer_id": offer.id,
                "branch_id": branch.id,
                "payment": payment_data,
                **usage_data,
                "message": "Offer redeemed successfully.",
            },
            status=status.HTTP_200_OK,
        )


class OfferAvailAPIView(APIView):
    """Single-step poster QR flow: scan + redeem in one call. No business dashboard needed."""

    permission_classes = [IsConsumerAccount]

    def post(self, request, offer_id):
        offer = get_object_or_404(Offer, pk=offer_id)
        serializer = OfferRedeemSerializer(
            data=request.data,
            context={"offer": offer, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        branch = serializer.validated_data["branch"]
        payment = serializer.validated_data["payment"]

        with transaction.atomic():
            locked_offer = Offer.objects.select_for_update().get(pk=offer.pk)
            usage, error = record_offer_redemption(
                request.user, locked_offer, branch, payment, record_scan=True
            )
            if usage is None:
                return Response(
                    {
                        "message": error,
                        "errors": {"non_field_errors": [error]},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            usage_data = OfferUsageSerializer.from_usage(locked_offer.id, usage).data

        payment_data = OfferPaymentPreviewSerializer.from_preview(payment).data
        return Response(
            {
                "errors": {},
                "offer_id": offer.id,
                "branch_id": branch.id,
                "payment": payment_data,
                **usage_data,
                "message": "Offer availed successfully. Show this screen at the counter.",
            },
            status=status.HTTP_200_OK,
        )
