from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

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


class MotivationRuleInput(serializers.Serializer):
    service_kind = serializers.CharField(max_length=16)
    fee_percent = serializers.DecimalField(max_digits=6, decimal_places=3, min_value=0, max_value=100)
    markup_percent = serializers.DecimalField(max_digits=6, decimal_places=3, min_value=0, max_value=100)
    commission_percent = serializers.DecimalField(max_digits=6, decimal_places=3, min_value=0, max_value=100)
    is_active = serializers.BooleanField(default=True)


class MotivationRulesView(APIView):
    def target(self, request):
        from accounts.models import User
        from accounts.permissions import has_permission

        user_id = request.query_params.get("user") or request.data.get("user")
        if not user_id:
            return None
        if str(user_id) != str(request.user.pk) and not has_permission(request.user, "settings.manage"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет доступа к мотивации другого сотрудника", status_code=403)
        user = User.objects.filter(pk=user_id, tenant_id=request.user.tenant_id).first()
        if user is None:
            raise ApiError(code="NOT_FOUND", message="Сотрудник не найден", status_code=404)
        return user

    def get(self, request):
        user = self.target(request)
        all_rules = MotivationRule.objects.filter(tenant_id=request.user.tenant_id)
        if request.query_params.get("history") != "1":
            all_rules = all_rules.filter(archived_at__isnull=True)
        rules = all_rules.filter(user=user)
        if user and not rules.exists():
            rules = all_rules.filter(user__isnull=True)
        return Response([{"id": str(rule.id), "user": str(rule.user_id) if rule.user_id else None, "service_kind": rule.service_kind, "fee_percent": str(rule.fee_percent), "markup_percent": str(rule.markup_percent), "commission_percent": str(rule.commission_percent), "is_active": rule.is_active, "updated_at": rule.updated_at, "archived_at": rule.archived_at} for rule in rules])

    def post(self, request):
        return self.save(request, replace=False)

    def put(self, request):
        return self.save(request, replace=True)

    def save(self, request, replace):
        from accounts.permissions import has_permission

        if not has_permission(request.user, "settings.manage"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права settings.manage", status_code=403)
        user = self.target(request)
        data = request.data.get("rules") if replace else [request.data]
        serializer = MotivationRuleInput(data=data, many=True)
        serializer.is_valid(raise_exception=True)
        kinds = [row["service_kind"] for row in serializer.validated_data]
        if len(kinds) != len(set(kinds)):
            raise ApiError(code="VALIDATION_ERROR", message="Виды услуг не должны повторяться", status_code=400)
        with transaction.atomic():
            if replace:
                MotivationRule.objects.filter(tenant_id=request.user.tenant_id, user=user, archived_at__isnull=True).update(archived_at=timezone.now(), updated_by=request.user)
            created = [MotivationRule.objects.create(tenant_id=request.user.tenant_id, user=user, created_by=request.user, **row) for row in serializer.validated_data]
            audit("workforce.motivation_rules_replaced", actor=request.user, resource=created[0] if created else None, request=request, after={"count": len(created), "user": str(user.pk) if user else None})
        if not replace:
            return Response({"id": str(created[0].pk)}, status=http.HTTP_201_CREATED)
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
