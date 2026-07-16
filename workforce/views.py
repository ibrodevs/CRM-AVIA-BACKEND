"""Смены, SLA queue, мотивация (ТЗ §19)."""
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers, status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from workforce.models import MotivationAccrual, MotivationRule, Shift, SlaInstance


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ["id", "user", "started_at", "ended_at", "opening_balance",
                  "closing_balance", "currency", "status", "closing_report",
                  "discrepancy_confirmed"]


class SlaQueueView(APIView):
    def get(self, request):
        now = timezone.now()
        qs = SlaInstance.objects.filter(
            tenant_id=request.user.tenant_id, resolved_at__isnull=True,
        ).select_related("assignee").order_by("response_deadline")
        if request.query_params.get("scope") != "team":
            qs = qs.filter(assignee=request.user)
        return Response([
            {"id": str(s.id), "resource_type": s.resource_type,
             "resource_id": s.resource_id,
             "assignee": str(s.assignee_id) if s.assignee_id else None,
             "response_deadline": s.response_deadline,
             "breached": bool(s.breached_at
                              or (s.response_deadline and s.response_deadline < now
                                  and s.responded_at is None)),
             "responded_at": s.responded_at}
            for s in qs[:100]
        ])


class ShiftCurrentView(APIView):
    def get(self, request):
        shift = Shift.objects.filter(user=request.user, status=Shift.Status.OPEN).first()
        if shift is None:
            return Response({"shift": None})
        return Response({"shift": ShiftSerializer(shift).data})


class ShiftStartView(APIView):
    def post(self, request):
        try:
            with transaction.atomic():
                shift = Shift.objects.create(
                    tenant_id=request.user.tenant_id, user=request.user,
                    started_at=timezone.now(),
                    opening_balance=request.data.get("opening_balance"),
                    currency=str(request.data.get("currency", "")),
                    created_by=request.user,
                )
        except IntegrityError:
            raise ApiError(code="SHIFT_ALREADY_OPEN",
                           message="У вас уже есть открытая смена",
                           status_code=409) from None
        audit("workforce.shift_started", actor=request.user, resource=shift,
              request=request)
        return Response(ShiftSerializer(shift).data, status=http.HTTP_201_CREATED)


def _build_shift_report(shift: Shift) -> dict:
    from django.db.models import Count, Sum

    operations = shift.operations.values("kind", "currency").annotate(
        count=Count("id"), total=Sum("amount"))
    return {
        "started_at": shift.started_at.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "operations": [
            {"kind": o["kind"], "currency": o["currency"], "count": o["count"],
             "total": str(o["total"] or 0)}
            for o in operations
        ],
    }


class ShiftPreviewCloseView(APIView):
    def post(self, request, shift_id):
        shift = Shift.objects.filter(pk=shift_id, user=request.user,
                                     status=Shift.Status.OPEN).first()
        if shift is None:
            raise ApiError(code="NOT_FOUND", message="Открытая смена не найдена",
                           status_code=404)
        return Response({"report": _build_shift_report(shift)})


class ShiftCloseView(APIView):
    def post(self, request, shift_id):
        with transaction.atomic():
            shift = Shift.objects.select_for_update().filter(
                pk=shift_id, user=request.user, status=Shift.Status.OPEN).first()
            if shift is None:
                raise ApiError(code="NOT_FOUND", message="Открытая смена не найдена",
                               status_code=404)
            closing_balance = request.data.get("closing_balance")
            report = _build_shift_report(shift)
            # расхождение баланса требует подтверждения (ТЗ §19)
            if shift.opening_balance is not None and closing_balance is not None:
                from decimal import Decimal

                declared = Decimal(str(closing_balance))
                if declared != shift.opening_balance and \
                        not request.data.get("confirm_discrepancy"):
                    raise ApiError(
                        code="DISCREPANCY_CONFIRMATION_REQUIRED",
                        message="Подтвердите расхождение баланса: confirm_discrepancy=true",
                        details={"opening": str(shift.opening_balance),
                                 "closing": str(declared)},
                        status_code=409,
                    )
                shift.discrepancy_confirmed = bool(
                    request.data.get("confirm_discrepancy"))
                shift.closing_balance = declared
            shift.status = Shift.Status.CLOSED
            shift.ended_at = timezone.now()
            shift.closing_report = report  # immutable snapshot
            shift.save()
        audit("workforce.shift_closed", actor=request.user, resource=shift,
              request=request)
        return Response(ShiftSerializer(shift).data)


class ShiftReportView(APIView):
    def get(self, request, shift_id):
        shift = Shift.objects.filter(pk=shift_id,
                                     tenant_id=request.user.tenant_id).first()
        if shift is None:
            raise ApiError(code="NOT_FOUND", message="Смена не найдена", status_code=404)
        if shift.user_id != request.user.pk and not request.user.is_superuser:
            from accounts.permissions import has_permission

            if not has_permission(request.user, "users.manage"):
                raise ApiError(code="PERMISSION_DENIED", message="Чужая смена",
                               status_code=403)
        return Response({"report": shift.closing_report or _build_shift_report(shift)})


class MotivationRulesView(APIView):
    permission_classes = [require("settings.manage")]

    def get(self, request):
        rules = MotivationRule.objects.filter(tenant_id=request.user.tenant_id,
                                              archived_at__isnull=True)
        return Response([
            {"id": str(r.id), "service_kind": r.service_kind,
             "fee_percent": str(r.fee_percent), "markup_percent": str(r.markup_percent),
             "commission_percent": str(r.commission_percent),
             "is_active": r.is_active}
            for r in rules
        ])

    def post(self, request):
        rule = MotivationRule.objects.create(
            tenant_id=request.user.tenant_id,
            service_kind=str(request.data.get("service_kind", "*")),
            fee_percent=request.data.get("fee_percent", 0),
            markup_percent=request.data.get("markup_percent", 0),
            commission_percent=request.data.get("commission_percent", 0),
            created_by=request.user,
        )
        audit("workforce.motivation_rule_created", actor=request.user, resource=rule,
              request=request)
        return Response({"id": str(rule.id)}, status=http.HTTP_201_CREATED)


class MotivationAccrualsView(APIView):
    def get(self, request):
        qs = MotivationAccrual.objects.filter(tenant_id=request.user.tenant_id)
        from accounts.permissions import has_permission

        if not has_permission(request.user, "users.manage"):
            qs = qs.filter(user=request.user)
        return Response([
            {"id": str(a.id), "user": str(a.user_id), "service": str(a.service_id),
             "amount": str(a.amount), "currency": a.currency,
             "reversed_at": a.reversed_at, "created_at": a.created_at}
            for a in qs.order_by("-created_at")[:200]
        ])
