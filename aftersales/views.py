from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from aftersales.models import (
    AFTERSALE_TRANSITIONS,
    AfterSaleCase,
    AfterSaleHistoryEntry,
    AfterSaleNumberCounter,
    AfterSaleQuote,
)
from common.audit import audit
from common.errors import ApiError, BusinessRejectionError, TransitionForbiddenError
from common.idempotency import idempotent_command
from common.money import quantize
from common.outbox import emit_event
from common.pagination import DefaultPagination
from documents.serializers import DocumentSerializer
from orders.selectors import get_order_or_404


class QuoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = AfterSaleQuote
        fields = [
            "id",
            "quote_version",
            "source",
            "currency",
            "original_paid",
            "supplier_penalty",
            "agency_service_fee",
            "other_withholdings",
            "refund_total",
            "old_itinerary",
            "new_itinerary",
            "exchange_difference",
            "details",
            "created_at",
        ]


class CaseSerializer(serializers.ModelSerializer):
    quotes = QuoteSerializer(many=True, read_only=True)

    class Meta:
        model = AfterSaleCase
        fields = [
            "id",
            "number",
            "order",
            "service",
            "type",
            "initiator",
            "responsible",
            "supplier",
            "status",
            "deadline",
            "currency",
            "financial_snapshot",
            "external_references",
            "current_quote",
            "client_approved_at",
            "client_approved_quote_version",
            "quotes",
            "created_at",
            "version",
        ]
        read_only_fields = [
            "id",
            "number",
            "status",
            "current_quote",
            "client_approved_at",
            "client_approved_quote_version",
            "created_at",
            "version",
        ]


def _get_case(request, case_id) -> AfterSaleCase:
    case = AfterSaleCase.objects.filter(pk=case_id, tenant_id=request.user.tenant_id).first()
    if case is None:
        raise ApiError(code="NOT_FOUND", message="Кейс не найден", status_code=404)
    return case


def _history(case, action, user, **details):
    AfterSaleHistoryEntry.objects.create(case=case, action=action, actor=user, details=details)


def _transition(case: AfterSaleCase, target: str, user, reason: str = "") -> None:
    allowed = AFTERSALE_TRANSITIONS.get(case.status, set())
    if target not in allowed:
        raise TransitionForbiddenError(
            code="AFTERSALE_TRANSITION_FORBIDDEN",
            message=f"Переход из {case.status} в {target} запрещён",
            details={"current_status": case.status, "allowed": sorted(allowed)},
        )
    old = case.status
    case.status = target
    case.version += 1
    case.updated_by = user
    case.save(update_fields=["status", "version", "updated_by", "updated_at"])
    _history(case, f"status:{target}", user, from_status=old, reason=reason)
    emit_event(
        "order.updated",
        case.order,
        payload={"action": "aftersale_status", "case": str(case.id), "to": target},
    )


class CaseListCreateView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = CaseSerializer

    def get(self, request):
        qs = AfterSaleCase.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True)
        params = request.query_params
        if case_type := params.get("type"):
            qs = qs.filter(type=case_type)
        if case_status := params.get("status"):
            qs = qs.filter(status=case_status)
        if order_id := params.get("order"):
            qs = qs.filter(order_id=order_id)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response(CaseSerializer(page, many=True).data)

    def post(self, request):
        needed = {
            "refund": "services.refund",
            "exchange": "services.exchange",
            "cancellation": "services.cancel",
            "certificate": "orders.change",
        }
        case_type = str(request.data.get("type", ""))
        permission = needed.get(case_type)
        if permission is None:
            raise ApiError(code="VALIDATION_ERROR", message=f"type из {sorted(needed)}", status_code=400)
        if not has_permission(request.user, permission):
            raise ApiError(code="PERMISSION_DENIED", message=f"Нет права {permission}", status_code=403)
        serializer = CaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        get_order_or_404(request.user, serializer.validated_data["order"].pk)
        with transaction.atomic():
            case = serializer.save(
                tenant_id=request.user.tenant_id,
                number=AfterSaleNumberCounter.next_number(request.user.tenant_id),
                responsible=serializer.validated_data.get("responsible") or request.user,
                created_by=request.user,
            )
            _history(case, "created", request.user, type=case_type)
            if case.service and case_type in ("refund", "exchange"):
                from services.models import OrderService

                service = case.service
                if case_type == "refund" and service.status == OrderService.Status.ISSUED:
                    service.status = OrderService.Status.REFUND_IN_PROGRESS
                    service.version += 1
                    service.save(update_fields=["status", "version", "updated_at"])
        audit("aftersales.case_created", actor=request.user, resource=case, request=request)
        return Response(CaseSerializer(case).data, status=http.HTTP_201_CREATED)


class CaseDetailView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, case_id):
        return Response(CaseSerializer(_get_case(request, case_id)).data)


class CaseQuoteView(APIView):
    """Новая версия quote; старое согласие клиента становится недействительным."""

    permission_classes = [require("orders.change")]

    def post(self, request, case_id):
        data = request.data
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            try:
                original_paid = Decimal(str(data.get("original_paid", "0")))
                penalty = Decimal(str(data.get("supplier_penalty", "0")))
                fee = Decimal(str(data.get("agency_service_fee", "0")))
                other = Decimal(str(data.get("other_withholdings", "0")))
            except InvalidOperation:
                raise ApiError(
                    code="VALIDATION_ERROR", message="Некорректные суммы", status_code=400
                ) from None
            currency = str(data.get("currency", case.currency))
            refund_total = max(quantize(original_paid - penalty - fee - other, currency), Decimal(0))
            last = case.quotes.order_by("-quote_version").first()
            quote = AfterSaleQuote.objects.create(
                tenant_id=case.tenant_id,
                case=case,
                quote_version=(last.quote_version + 1) if last else 1,
                source=str(data.get("source", "manual")),
                currency=currency,
                original_paid=original_paid,
                supplier_penalty=penalty,
                agency_service_fee=fee,
                other_withholdings=other,
                refund_total=refund_total,
                old_itinerary=data.get("old_itinerary"),
                new_itinerary=data.get("new_itinerary"),
                exchange_difference=data.get("exchange_difference"),
                details=data.get("details", {}),
                created_by=request.user,
            )
            case.current_quote = quote
            case.currency = currency

            if case.client_approved_at is not None:
                case.client_approved_at = None
                case.client_approved_quote_version = None
                _history(
                    case, "client_approval_invalidated", request.user, new_quote_version=quote.quote_version
                )
            case.save(
                update_fields=[
                    "current_quote",
                    "currency",
                    "client_approved_at",
                    "client_approved_quote_version",
                    "updated_at",
                ]
            )
            _history(
                case,
                "quote_created",
                request.user,
                quote_version=quote.quote_version,
                refund_total=str(refund_total),
            )
        audit("aftersales.quote_created", actor=request.user, resource=case, request=request)
        return Response(QuoteSerializer(quote).data, status=http.HTTP_201_CREATED)


class CaseTransitionView(APIView):
    permission_classes = [require("orders.change_status", "orders.change")]

    def post(self, request, case_id):
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            _transition(
                case,
                str(request.data.get("target_status", "")),
                request.user,
                reason=str(request.data.get("reason", "")),
            )
        return Response(CaseSerializer(case).data)


class CaseSendForApprovalView(APIView):
    permission_classes = [require("orders.change")]

    def post(self, request, case_id):
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            if case.current_quote_id is None:
                raise ApiError(code="QUOTE_REQUIRED", message="Сначала создайте quote", status_code=409)
            _transition(case, AfterSaleCase.Status.AWAITING_CLIENT_APPROVAL, request.user)
        return Response(CaseSerializer(case).data)


class CaseClientApproveView(APIView):
    """Фиксация согласия клиента с конкретной версией расчёта (ТЗ §16)."""

    permission_classes = [require("orders.change")]

    def post(self, request, case_id):
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            if case.status != AfterSaleCase.Status.AWAITING_CLIENT_APPROVAL:
                raise ApiError(
                    code="INVALID_STATUS", message="Кейс не ожидает согласия клиента", status_code=409
                )
            quote_version = request.data.get("quote_version")
            if case.current_quote is None or int(quote_version or 0) != case.current_quote.quote_version:
                raise BusinessRejectionError(
                    code="QUOTE_VERSION_MISMATCH",
                    message="Согласие относится не к актуальной версии расчёта",
                    details={
                        "current_version": case.current_quote.quote_version if case.current_quote else None
                    },
                )
            case.client_approved_at = timezone.now()
            case.client_approved_quote_version = case.current_quote.quote_version
            case.save(update_fields=["client_approved_at", "client_approved_quote_version", "updated_at"])
            _history(case, "client_approved", request.user, quote_version=case.client_approved_quote_version)
        return Response(CaseSerializer(case).data)


class CaseSubmitToSupplierView(APIView):
    permission_classes = [require("orders.change")]

    @idempotent_command("aftersales.submit")
    def post(self, request, case_id):
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            if case.type in ("refund", "exchange") and case.client_approved_at is None:
                raise BusinessRejectionError(
                    code="CLIENT_APPROVAL_REQUIRED",
                    message="Нужно согласие клиента с актуальным расчётом",
                )
            _transition(case, AfterSaleCase.Status.SUBMITTED_TO_SUPPLIER, request.user)
            _transition(case, AfterSaleCase.Status.PROCESSING, request.user)
        return Response(CaseSerializer(case).data)


class CaseExecuteView(APIView):
    """Завершение кейса: финансовая операция обязательна (ТЗ §16)."""

    permission_classes = [require("finance.refund", "orders.change_status")]

    @idempotent_command("aftersales.execute")
    def post(self, request, case_id):
        from finance import services as finance_service
        from services.models import OrderService

        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            if case.status != AfterSaleCase.Status.PROCESSING:
                raise ApiError(
                    code="INVALID_STATUS", message="Завершать можно только кейс в processing", status_code=409
                )
            quote = case.current_quote
            manual_exception = bool(request.data.get("manual_exception"))
            if case.type == "refund":
                if quote is None and not manual_exception:
                    raise BusinessRejectionError(
                        code="FINANCIAL_OPERATION_REQUIRED",
                        message="Возврат нельзя завершить без подтверждённого расчёта "
                        "или approved manual exception",
                    )
                if quote is not None:
                    refund = finance_service.build_refund(
                        tenant_id=case.tenant_id,
                        currency=quote.currency,
                        original_paid=quote.original_paid,
                        supplier_penalty=quote.supplier_penalty,
                        agency_service_fee=quote.agency_service_fee,
                        other_withholdings=quote.other_withholdings,
                        aftersale_case=case,
                        user=request.user,
                    )
                    finance_service.execute_refund(refund_id=refund.pk, user=request.user, request=request)
                    case.financial_snapshot = refund.formula_snapshot
                elif manual_exception:
                    if not request.data.get("reason"):
                        raise ApiError(
                            code="REASON_REQUIRED",
                            message="Manual exception требует причины",
                            status_code=400,
                        )
                    _history(case, "manual_exception", request.user, reason=request.data["reason"])
            _transition(case, AfterSaleCase.Status.COMPLETED, request.user)
            if case.service:
                service = OrderService.objects.select_for_update().get(pk=case.service_id)
                target = {
                    "refund": OrderService.Status.REFUNDED,
                    "cancellation": OrderService.Status.CANCELLED,
                }.get(case.type)
                if target:
                    service.status = target
                    service.version += 1
                    service.save(update_fields=["status", "version", "updated_at"])
            case.save(update_fields=["financial_snapshot", "updated_at"])
        audit("aftersales.case_executed", actor=request.user, resource=case, request=request)
        return Response(CaseSerializer(case).data)


class CaseCancelView(APIView):
    permission_classes = [require("orders.change")]

    def post(self, request, case_id):
        with transaction.atomic():
            case = AfterSaleCase.objects.select_for_update().get(pk=_get_case(request, case_id).pk)
            _transition(
                case, AfterSaleCase.Status.CANCELLED, request.user, reason=str(request.data.get("reason", ""))
            )
        return Response(CaseSerializer(case).data)


class CaseDocumentsView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request, case_id):
        case = _get_case(request, case_id)
        documents = case.order.documents.filter(archived_at__isnull=True)
        return Response(DocumentSerializer(documents, many=True).data)


class CaseHistoryView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, case_id):
        case = _get_case(request, case_id)
        return Response(
            [
                {
                    "id": h.id,
                    "action": h.action,
                    "actor": str(h.actor_id) if h.actor_id else None,
                    "details": h.details,
                    "created_at": h.created_at,
                }
                for h in case.history.all()
            ]
        )
