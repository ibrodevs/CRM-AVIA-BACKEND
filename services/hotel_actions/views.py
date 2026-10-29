from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from aftersales.models import HotelOperation
from common.errors import ApiError
from common.locking import check_version
from services.hotel_actions.operations import confirm_operation, create_operation, record, require_permission
from services.hotel_actions.presentation import booking_summary, capabilities, permissions


class OperationInput(serializers.Serializer):
    operation_type = serializers.CharField(max_length=40)
    scope = serializers.ChoiceField(choices=["booking", "rooms"], default="booking")
    room_refs = serializers.ListField(
        child=serializers.CharField(max_length=128), max_length=200, default=list
    )
    requested_changes = serializers.DictField(default=dict)

    def validate_requested_changes(self, value):
        # User requests are instructions for an operator, never supplier payloads.
        unknown = set(value) - {"description", "cancellation_mode"}
        if (
            unknown
            or not isinstance(value.get("description", ""), str)
            or len(value.get("description", "")) > 5000
        ):
            raise serializers.ValidationError(
                "Допустимы описание запроса (до 5000 символов) и cancellation_mode"
            )
        if value.get("cancellation_mode") not in (None, "penalty"):
            raise serializers.ValidationError(
                "Отмена без штрафа требует подтверждённого расчёта поставщика; используйте ручной запрос"
            )
        return value


class OperationSerializer(serializers.ModelSerializer):
    history = serializers.SerializerMethodField()
    actor_name = serializers.CharField(source="actor.full_name", read_only=True, default="")

    class Meta:
        model = HotelOperation
        fields = [
            "id",
            "operation_type",
            "scope",
            "room_refs",
            "status",
            "execution_mode",
            "original_snapshot",
            "requested_changes",
            "result_snapshot",
            "financial_snapshot",
            "supplier_response",
            "supplier_reference",
            "actor_name",
            "created_at",
            "updated_at",
            "confirmed_at",
            "completed_at",
            "version",
            "history",
        ]

    def get_history(self, obj):
        return [
            {
                "id": row.id,
                "action": row.action,
                "actor": str(row.actor_id) if row.actor_id else None,
                "created_at": row.created_at,
                "before": row.before,
                "after": row.after,
            }
            for row in obj.history.all()
        ]


def hotel_service(request, service_id):
    from services.views import _get_service

    service = _get_service(request, service_id)
    if service.kind != "hotel" or service.archived_at:
        raise ApiError(code="NOT_FOUND", message="Гостиничная услуга не найдена", status_code=404)
    return service


class HotelActionsView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, service_id):
        service = hotel_service(request, service_id)
        return Response(
            {
                "summary": booking_summary(service),
                "capabilities": capabilities(service),
                "permissions": permissions(request.user),
                "operations": OperationSerializer(
                    service.hotel_operations.prefetch_related("history")
                    .select_related("actor")
                    .order_by("-created_at"),
                    many=True,
                ).data,
            }
        )

    def post(self, request, service_id):
        service = hotel_service(request, service_id)
        serializer = OperationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        operation = create_operation(
            service,
            request.user,
            serializer.validated_data,
            request.headers.get("Idempotency-Key", "").strip(),
        )
        return Response(OperationSerializer(operation).data, status=201)


class HotelOperationConfirmView(APIView):
    permission_classes = [require("orders.view")]

    def post(self, request, service_id, operation_id):
        operation = confirm_operation(
            hotel_service(request, service_id), operation_id, request.user, request.data
        )
        return Response(OperationSerializer(operation).data, status=202)


class HotelOperationResolveView(APIView):
    permission_classes = [require("orders.view")]

    def post(self, request, service_id, operation_id):
        service = hotel_service(request, service_id)
        require_permission(request.user, "modify")
        with transaction.atomic():
            operation = (
                HotelOperation.objects.select_for_update().filter(service=service, pk=operation_id).first()
            )
            if not operation:
                raise ApiError(code="NOT_FOUND", message="Операция не найдена", status_code=404)
            if operation.execution_mode != "manual":
                raise ApiError(
                    code="STATUS_INQUIRY_REQUIRED",
                    message="API-результат проверяется только у поставщика",
                    status_code=409,
                )
            check_version(operation, request.data.get("version"))
            transitions = {
                "created": {"checking", "rejected"},
                "checking": {"awaiting_supplier", "completed", "rejected"},
                "awaiting_supplier": {"completed", "rejected"},
            }
            target = request.data.get("status")
            note = request.data.get("note")
            if (
                target not in transitions.get(operation.status, set())
                or not isinstance(note, str)
                or not note.strip()
                or len(note) > 5000
            ):
                raise ApiError(
                    code="INVALID_TRANSITION",
                    message="Укажите допустимый статус и комментарий (до 5000 символов)",
                    status_code=400,
                )
            before = {"status": operation.status}
            operation.status = target
            operation.result_snapshot = {"operator_note": note.strip(), "booking_unchanged": True}
            operation.completed_at = timezone.now() if target in ("completed", "rejected") else None
            operation.version += 1
            operation.save(
                update_fields=["status", "result_snapshot", "completed_at", "version", "updated_at"]
            )
            record(operation, "operator_update", before=before, actor=request.user)
        return Response(OperationSerializer(operation).data)
