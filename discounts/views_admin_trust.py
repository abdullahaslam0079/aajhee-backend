"""Admin trust / ops endpoints: reports, audit logs, private media, verify, order patch."""

from __future__ import annotations

import mimetypes

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .audit_utils import write_audit
from .models import (
    AuditLog,
    Business,
    Order,
    OrderProblemReport,
    OrderProblemReportNote,
)
from .notification_utils import notify_merchant_verification
from .permissions import IsAdminAccount, IsAdminOwner, admin_can_write_orders_reports
from .private_media import (
    PRIVATE_MEDIA_KINDS,
    build_signed_private_media_url,
    business_private_file,
    get_business_for_private_media,
    verify_private_media_token,
)
from .serializers_admin import (
    AdminAuditLogSerializer,
    AdminOrderPatchSerializer,
    AdminReportDetailSerializer,
    AdminReportListSerializer,
    AdminReportNoteCreateSerializer,
    AdminReportResolveSerializer,
    AdminReportStatusSerializer,
    AdminVerifyBusinessSerializer,
)
from .serializers_commerce import OrderSerializer


def _paginate(queryset, request, serializer_class, context=None):
    from .pagination import page_payload, parse_page_params, slice_queryset

    page, page_size = parse_page_params(request)
    total, items = slice_queryset(queryset, page, page_size)
    serializer = serializer_class(
        items, many=True, context=context or {"request": request}
    )
    return Response(
        page_payload(
            count=total,
            page=page,
            page_size=page_size,
            results=serializer.data,
        )
    )


def _verification_checklist_errors(business: Business) -> dict[str, list[str]]:
    """Required before approve/verify: phone, WhatsApp, CNIC, and shop photo or Instagram."""
    errors: dict[str, list[str]] = {}
    if not (business.phone or "").strip():
        errors["phone"] = ["Business phone is required for verification."]
    if not (business.notification_whatsapp or "").strip():
        errors["notification_whatsapp"] = [
            "WhatsApp / notification number is required for verification."
        ]
    if not business.cnic_image:
        errors["cnic_image"] = ["CNIC image is required for verification."]
    has_shop = bool(business.shop_photo) or bool((business.instagram_url or "").strip())
    if not has_shop:
        errors["shop_photo"] = [
            "Upload a shop photo or provide an Instagram URL for verification."
        ]
        errors["instagram_url"] = [
            "Upload a shop photo or provide an Instagram URL for verification."
        ]
    return errors


class AdminReportListAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_queryset(self):
        return (
            OrderProblemReport.objects.select_related(
                "order", "order__business", "order__branch", "user"
            )
            .prefetch_related("notes")
            .order_by("-created_at", "-id")
        )

    def get(self, request):
        qs = self.get_queryset()
        status_filter = (request.query_params.get("status") or "").strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        return _paginate(qs, request, AdminReportListSerializer)

    def patch(self, request):
        """Bulk-style status update via body {id, status} — prefer detail resolve/notes."""
        if not admin_can_write_orders_reports(request.user):
            return Response(
                {"detail": "Admin account required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report_id = request.data.get("id")
        if not report_id:
            return Response(
                {"message": "Report id is required.", "errors": {"id": ["Required."]}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        report = get_object_or_404(self.get_queryset(), pk=report_id)
        serializer = AdminReportStatusSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]
        report.status = new_status
        if new_status == OrderProblemReport.Status.RESOLVED and not report.resolved_at:
            report.resolved_at = timezone.now()
            report.resolved_by = request.user
        report.save()
        write_audit(
            actor=request.user,
            action="report.status_change",
            target_type="OrderProblemReport",
            target_id=report.id,
            metadata={"status": new_status},
        )
        return Response(AdminReportListSerializer(report).data)


class AdminReportDetailAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get_object(self, report_id: int) -> OrderProblemReport:
        return get_object_or_404(
            OrderProblemReport.objects.select_related(
                "order", "order__business", "order__branch", "user", "resolved_by"
            ).prefetch_related("notes__author"),
            pk=report_id,
        )

    def get(self, request, report_id: int):
        report = self.get_object(report_id)
        return Response(
            AdminReportDetailSerializer(report, context={"request": request}).data
        )

    def patch(self, request, report_id: int):
        if not admin_can_write_orders_reports(request.user):
            return Response(
                {"detail": "Admin account required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report = self.get_object(report_id)
        serializer = AdminReportStatusSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]
        report.status = new_status
        if new_status == OrderProblemReport.Status.RESOLVED and not report.resolved_at:
            report.resolved_at = timezone.now()
            report.resolved_by = request.user
        elif new_status != OrderProblemReport.Status.RESOLVED:
            report.resolved_at = None
            report.resolved_by = None
        report.save()
        write_audit(
            actor=request.user,
            action="report.status_change",
            target_type="OrderProblemReport",
            target_id=report.id,
            metadata={"status": new_status},
        )
        return Response(
            AdminReportDetailSerializer(report, context={"request": request}).data
        )


class AdminReportNoteCreateAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request, report_id: int):
        if not admin_can_write_orders_reports(request.user):
            return Response(
                {"detail": "Admin account required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report = get_object_or_404(OrderProblemReport, pk=report_id)
        serializer = AdminReportNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = OrderProblemReportNote.objects.create(
            report=report,
            author=request.user,
            body=serializer.validated_data["body"],
        )
        if report.status == OrderProblemReport.Status.OPEN:
            report.status = OrderProblemReport.Status.IN_PROGRESS
            report.save(update_fields=["status", "updated_at"])
        write_audit(
            actor=request.user,
            action="report.note",
            target_type="OrderProblemReport",
            target_id=report.id,
            metadata={"note_id": note.id},
        )
        from .serializers_admin import AdminReportNoteSerializer

        return Response(
            AdminReportNoteSerializer(note).data,
            status=status.HTTP_201_CREATED,
        )


class AdminReportResolveAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def post(self, request, report_id: int):
        if not admin_can_write_orders_reports(request.user):
            return Response(
                {"detail": "Admin account required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report = get_object_or_404(OrderProblemReport, pk=report_id)
        serializer = AdminReportResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report.status = OrderProblemReport.Status.RESOLVED
        report.resolution_note = serializer.validated_data["resolution_note"]
        report.resolved_at = timezone.now()
        report.resolved_by = request.user
        report.save(
            update_fields=[
                "status",
                "resolution_note",
                "resolved_at",
                "resolved_by",
                "updated_at",
            ]
        )
        write_audit(
            actor=request.user,
            action="report.resolve",
            target_type="OrderProblemReport",
            target_id=report.id,
            metadata={},
        )
        return Response(
            AdminReportDetailSerializer(report, context={"request": request}).data
        )


class AdminAuditLogListAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request):
        qs = AuditLog.objects.select_related("actor").order_by("-created_at", "-id")
        action = (request.query_params.get("action") or "").strip()
        if action:
            qs = qs.filter(action=action)
        target_type = (request.query_params.get("target_type") or "").strip()
        if target_type:
            qs = qs.filter(target_type=target_type)
        target_id = (request.query_params.get("target_id") or "").strip()
        if target_id:
            qs = qs.filter(target_id=target_id)
        return _paginate(qs, request, AdminAuditLogSerializer)


class AdminBusinessPrivateMediaURLAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def get(self, request, business_id: int, kind: str):
        if kind not in PRIVATE_MEDIA_KINDS:
            raise Http404("Unknown media kind.")
        business = get_business_for_private_media(business_id)
        url = build_signed_private_media_url(request, business, kind)
        return Response({"url": url, "kind": kind, "expires_in": 300})


class AdminBusinessPrivateMediaFileAPIView(APIView):
    """Local signed download — allow token auth without admin session."""

    authentication_classes = []
    permission_classes = []

    def get(self, request, business_id: int, kind: str):
        if kind not in PRIVATE_MEDIA_KINDS:
            raise Http404("Unknown media kind.")
        token = (request.query_params.get("token") or "").strip()
        if not token or not verify_private_media_token(token, business_id, kind):
            return Response(
                {"detail": "Invalid or expired token."},
                status=status.HTTP_403_FORBIDDEN,
            )
        business = get_business_for_private_media(business_id)
        field = business_private_file(business, kind)
        content_type, _ = mimetypes.guess_type(field.name)
        try:
            file_handle = field.open("rb")
        except Exception as exc:
            raise Http404("File not found.") from exc
        return FileResponse(
            file_handle,
            content_type=content_type or "application/octet-stream",
            as_attachment=False,
            filename=field.name.rsplit("/", 1)[-1],
        )


class AdminBusinessVerifyAPIView(APIView):
    permission_classes = [IsAdminAccount, IsAdminOwner]

    def post(self, request, business_id: int):
        business = get_object_or_404(
            Business.objects.select_related("owner").filter(deleted_at__isnull=True),
            pk=business_id,
        )
        serializer = AdminVerifyBusinessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        reason = serializer.validated_data.get("reason") or ""

        if action == "approve":
            errors = _verification_checklist_errors(business)
            if errors:
                return Response(
                    {
                        "message": "Business is missing required verification fields.",
                        "errors": errors,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            previous = business.verification_status
            business.verification_status = Business.VerificationStatus.VERIFIED
            business.is_paused = False
            business.save(update_fields=["verification_status", "is_paused"])
            write_audit(
                actor=request.user,
                action="business.verify",
                target_type="Business",
                target_id=business.id,
                metadata={"from": previous, "to": business.verification_status},
            )
            notify_merchant_verification(business, approved=True, reason=reason)
            return Response(
                {
                    "message": "Business verified.",
                    "errors": {},
                    "verification_status": business.verification_status,
                }
            )

        # reject
        previous = business.verification_status
        business.verification_status = Business.VerificationStatus.SUSPENDED
        business.save(update_fields=["verification_status"])
        write_audit(
            actor=request.user,
            action="business.reject",
            target_type="Business",
            target_id=business.id,
            metadata={
                "from": previous,
                "to": business.verification_status,
                "reason": reason,
            },
        )
        notify_merchant_verification(business, approved=False, reason=reason)
        return Response(
            {
                "message": "Business rejected / suspended.",
                "errors": {},
                "verification_status": business.verification_status,
            }
        )


class AdminOrderPatchAPIView(APIView):
    permission_classes = [IsAdminAccount]

    def patch(self, request, public_id):
        if not admin_can_write_orders_reports(request.user):
            return Response(
                {"detail": "Admin account required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        order = get_object_or_404(
            Order.objects.select_related("business", "branch", "user").prefetch_related(
                "items", "payment_proofs", "delivery_snapshot", "status_history"
            ),
            public_id=public_id,
        )
        serializer = AdminOrderPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        update_fields = ["updated_at"]
        if "admin_note" in serializer.validated_data:
            order.admin_note = serializer.validated_data["admin_note"]
            update_fields.append("admin_note")
        if "is_escalated" in serializer.validated_data:
            order.is_escalated = serializer.validated_data["is_escalated"]
            update_fields.append("is_escalated")
        order.save(update_fields=update_fields)
        write_audit(
            actor=request.user,
            action="order.admin_patch",
            target_type="Order",
            target_id=str(order.public_id),
            metadata={
                k: serializer.validated_data[k]
                for k in ("admin_note", "is_escalated")
                if k in serializer.validated_data
            },
        )
        return Response(OrderSerializer(order, context={"request": request}).data)


# Re-export checklist helper for views_admin PATCH verification.
verification_checklist_errors = _verification_checklist_errors
