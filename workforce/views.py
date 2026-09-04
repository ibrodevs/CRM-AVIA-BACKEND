from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from common.pagination import DefaultPagination
from workforce.models import MotivationAccrual, MotivationRule, Shift, SlaInstance


class ShiftSerializer(serializers.ModelSerializer):
    operations = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = [
            "id",
            "user",
            "started_at",
            "ended_at",
            "opening_balance",
            "closing_balance",
            "currency",
            "status",
            "closing_report",
            "discrepancy_confirmed",
            "operations",
        ]

    def get_operations(self, obj):
        return [
            {
                "id": operation.id,
                "kind": operation.kind,
                "resource_type": operation.resource_type,
                "resource_id": operation.resource_id,
                "amount": str(operation.amount) if operation.amount is not None else None,
                "currency": operation.currency,
                "created_at": operation.created_at,
            }
            for operation in obj.operations.all().order_by("created_at")
        ]


class SlaQueueView(APIView):
    def get(self, request):
        now = timezone.now()
        qs = (
            SlaInstance.objects.filter(
                tenant_id=request.user.tenant_id,
                resolved_at__isnull=True,
            )
            .select_related("assignee", "policy")
            .order_by("response_deadline")
        )
        if request.query_params.get("scope") != "team":
            qs = qs.filter(assignee=request.user)
        return Response(
            [
                {
                    "id": str(s.id),
                    "resource_type": s.resource_type,
                    "resource_id": s.resource_id,
                    "assignee": str(s.assignee_id) if s.assignee_id else None,
                    "response_deadline": s.response_deadline,
                    "started_at": s.started_at,
                    "limit_minutes": s.policy.response_minutes,
                    "breached": bool(
                        s.breached_at
                        or (s.response_deadline and s.response_deadline < now and s.responded_at is None)
                    ),
                    "responded_at": s.responded_at,
                }
                for s in qs[:100]
            ]
        )


class ShiftCurrentView(APIView):
    def get(self, request):
        shift = Shift.objects.filter(user=request.user, status=Shift.Status.OPEN).first()
        if shift is None:
            return Response({"shift": None})
        return Response({"shift": ShiftSerializer(shift).data})


class ShiftListView(APIView):
    """История реальных смен текущего пользователя или выбранного оператора."""

    def get(self, request):
        from accounts.permissions import has_permission

        qs = Shift.objects.filter(tenant_id=request.user.tenant_id).prefetch_related("operations")
        user_id = request.query_params.get("user")
        if has_permission(request.user, "users.manage") and user_id:
            qs = qs.filter(user_id=user_id)
        else:
            qs = qs.filter(user=request.user)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(qs.order_by("-started_at"), request, view=self)
        return paginator.get_paginated_response(ShiftSerializer(page, many=True).data)


class ShiftStartView(APIView):
    def post(self, request):
        try:
            with transaction.atomic():
                shift = Shift.objects.create(
                    tenant_id=request.user.tenant_id,
                    user=request.user,
                    started_at=timezone.now(),
                    opening_balance=request.data.get("opening_balance"),
                    currency=str(request.data.get("currency", "")),
                    created_by=request.user,
                )
        except IntegrityError:
            raise ApiError(
                code="SHIFT_ALREADY_OPEN", message="У вас уже есть открытая смена", status_code=409
            ) from None
        audit("workforce.shift_started", actor=request.user, resource=shift, request=request)
        return Response(ShiftSerializer(shift).data, status=http.HTTP_201_CREATED)


def _build_shift_report(shift: Shift) -> dict:
    from django.db.models import Count, Sum

    operations = shift.operations.values("kind", "currency").annotate(count=Count("id"), total=Sum("amount"))
    return {
        "started_at": shift.started_at.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "operations": [
            {"kind": o["kind"], "currency": o["currency"], "count": o["count"], "total": str(o["total"] or 0)}
            for o in operations
        ],
    }


class ShiftPreviewCloseView(APIView):
    def post(self, request, shift_id):
        shift = Shift.objects.filter(pk=shift_id, user=request.user, status=Shift.Status.OPEN).first()
        if shift is None:
            raise ApiError(code="NOT_FOUND", message="Открытая смена не найдена", status_code=404)
        return Response({"report": _build_shift_report(shift)})


class ShiftCloseView(APIView):
    def post(self, request, shift_id):
        with transaction.atomic():
            shift = (
                Shift.objects.select_for_update()
                .filter(pk=shift_id, user=request.user, status=Shift.Status.OPEN)
                .first()
            )
            if shift is None:
                raise ApiError(code="NOT_FOUND", message="Открытая смена не найдена", status_code=404)
            closing_balance = request.data.get("closing_balance")
            report = _build_shift_report(shift)

            if shift.opening_balance is not None and closing_balance is not None:
                from decimal import Decimal

                declared = Decimal(str(closing_balance))
                if declared != shift.opening_balance and not request.data.get("confirm_discrepancy"):
                    raise ApiError(
                        code="DISCREPANCY_CONFIRMATION_REQUIRED",
                        message="Подтвердите расхождение баланса: confirm_discrepancy=true",
                        details={"opening": str(shift.opening_balance), "closing": str(declared)},
                        status_code=409,
                    )
                shift.discrepancy_confirmed = bool(request.data.get("confirm_discrepancy"))
                shift.closing_balance = declared
            shift.status = Shift.Status.CLOSED
            shift.ended_at = timezone.now()
            shift.closing_report = report
            shift.save()
        audit("workforce.shift_closed", actor=request.user, resource=shift, request=request)
        return Response(ShiftSerializer(shift).data)


class ShiftReportView(APIView):
    def get(self, request, shift_id):
        shift = Shift.objects.filter(pk=shift_id, tenant_id=request.user.tenant_id).first()
        if shift is None:
            raise ApiError(code="NOT_FOUND", message="Смена не найдена", status_code=404)
        if shift.user_id != request.user.pk and not request.user.is_superuser:
            from accounts.permissions import has_permission

            if not has_permission(request.user, "users.manage"):
                raise ApiError(code="PERMISSION_DENIED", message="Чужая смена", status_code=403)
        return Response({"report": shift.closing_report or _build_shift_report(shift)})


class MotivationRulesView(APIView):
    def get(self, request):
        rules = MotivationRule.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True)
        return Response(
            [
                {
                    "id": str(r.id),
                    "service_kind": r.service_kind,
                    "fee_percent": str(r.fee_percent),
                    "markup_percent": str(r.markup_percent),
                    "commission_percent": str(r.commission_percent),
                    "is_active": r.is_active,
                    "updated_at": r.updated_at,
                }
                for r in rules
            ]
        )

    def post(self, request):
        from accounts.permissions import has_permission

        if not has_permission(request.user, "settings.manage"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права settings.manage", status_code=403)
        rule = MotivationRule.objects.create(
            tenant_id=request.user.tenant_id,
            service_kind=str(request.data.get("service_kind", "*")),
            fee_percent=request.data.get("fee_percent", 0),
            markup_percent=request.data.get("markup_percent", 0),
            commission_percent=request.data.get("commission_percent", 0),
            created_by=request.user,
        )
        audit("workforce.motivation_rule_created", actor=request.user, resource=rule, request=request)
        return Response({"id": str(rule.id)}, status=http.HTTP_201_CREATED)

    def put(self, request):
        from accounts.permissions import has_permission

        if not has_permission(request.user, "settings.manage"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права settings.manage", status_code=403)
        rows = request.data.get("rules")
        if not isinstance(rows, list):
            raise ApiError(code="VALIDATION_ERROR", message="rules должен быть массивом", status_code=400)
        created = []
        with transaction.atomic():
            MotivationRule.objects.filter(
                tenant_id=request.user.tenant_id, archived_at__isnull=True
            ).update(archived_at=timezone.now(), updated_by=request.user)
            for row in rows:
                created.append(
                    MotivationRule.objects.create(
                        tenant_id=request.user.tenant_id,
                        service_kind=str(row.get("service_kind", "*")),
                        fee_percent=row.get("fee_percent", 0),
                        markup_percent=row.get("markup_percent", 0),
                        commission_percent=row.get("commission_percent", 0),
                        is_active=bool(row.get("is_active", True)),
                        created_by=request.user,
                    )
                )
        audit(
            "workforce.motivation_rules_replaced",
            actor=request.user,
            resource=created[0] if created else None,
            request=request,
            after={"count": len(created)},
        )
        return self.get(request)


class MotivationAccrualsView(APIView):
    def get(self, request):
        qs = MotivationAccrual.objects.filter(tenant_id=request.user.tenant_id)
        from accounts.permissions import has_permission

        if not has_permission(request.user, "users.manage"):
            qs = qs.filter(user=request.user)
        elif user_id := request.query_params.get("user"):
            qs = qs.filter(user_id=user_id)
        if date_from := request.query_params.get("from"):
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to := request.query_params.get("to"):
            qs = qs.filter(created_at__date__lte=date_to)
        return Response(
            [
                {
                    "id": str(a.id),
                    "user": str(a.user_id),
                    "service": str(a.service_id),
                    "amount": str(a.amount),
                    "currency": a.currency,
                    "reversed_at": a.reversed_at,
                    "created_at": a.created_at,
                }
                for a in qs.order_by("-created_at")[:200]
            ]
        )
