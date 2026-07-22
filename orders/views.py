from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.idempotency import idempotent_command
from common.locking import check_version
from common.outbox import emit_event
from common.pagination import DefaultPagination
from orders import services as order_service
from orders.models import Order, OrderStatusHistory
from orders.selectors import filter_orders, get_order_or_404, orders_visible_to
from orders.serializers import (
    OrderCreateSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    OrderTaskSerializer,
    ParticipantSerializer,
    RoutePointSerializer,
    RouteSerializer,
    order_finance_summary,
)


class OrderListCreateView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = OrderListSerializer

    def get(self, request):
        qs = (
            orders_visible_to(request.user)
            .select_related("client_person", "client_company", "operator")
            .prefetch_related("services")
            .order_by("-created_at")
        )
        qs = filter_orders(qs, request.query_params)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(OrderListSerializer(page, many=True).data)

    def post(self, request):
        if not has_permission(request.user, "orders.create"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.create", status_code=403)
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        order = order_service.create_order(
            tenant_id=request.user.tenant_id, user=request.user, data=data, request=request
        )
        return Response(OrderDetailSerializer(order).data, status=http.HTTP_201_CREATED)


class OrderDetailView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        return Response(OrderDetailSerializer(order).data)

    def patch(self, request, order_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        with transaction.atomic():
            order = (
                Order.objects.select_for_update().filter(pk=get_order_or_404(request.user, order_id).pk).get()
            )
            check_version(order, request.data.get("version"))
            allowed = {
                "priority",
                "preferred_channel",
                "purpose",
                "comment",
                "planned_start",
                "planned_end",
                "contact_person",
                "source",
            }
            forbidden = set(request.data) & {"status", "stage", "number", "operator"}
            if forbidden:
                raise ApiError(
                    code="FIELD_NOT_PATCHABLE",
                    message="Статус/этап/номер/оператор меняются командами, не PATCH",
                    fields={f: ["Используйте командный endpoint"] for f in forbidden},
                    status_code=400,
                )
            data = {k: v for k, v in request.data.items() if k in allowed}
            serializer = OrderDetailSerializer(order, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            order.version += 1
            order.updated_by = request.user
            serializer.save(version=order.version, updated_by=request.user)
            emit_event("order.updated", order, payload={"action": "patched"})
            audit(
                "order.updated",
                actor=request.user,
                resource=order,
                request=request,
                after={k: str(v) for k, v in data.items()},
            )
        return Response(OrderDetailSerializer(order).data)


class OrderTransitionView(APIView):
    permission_classes = [require("orders.change_status")]

    @idempotent_command("orders.transition", required=False)
    def post(self, request, order_id):
        get_order_or_404(request.user, order_id)
        order = order_service.transition_order(
            order_id=order_id,
            user=request.user,
            target_status=str(request.data.get("target_status", "")),
            reason=str(request.data.get("reason", "")),
            expected_version=request.data.get("version"),
            request=request,
        )
        data = OrderDetailSerializer(order).data
        data["audit_event_id"] = getattr(order, "_audit_event_id", None)
        return Response(data)


class OrderCancelView(APIView):
    permission_classes = [require("orders.change_status")]

    @idempotent_command("orders.cancel")
    def post(self, request, order_id):
        order = get_order_or_404(request.user, order_id)

        active = order.services.filter(status__in=["booked", "confirmed", "issued"])
        if active.exists() and not request.data.get("confirm_cancellation"):
            raise ApiError(
                code="ORDER_HAS_ACTIVE_BOOKINGS",
                message="У заказа есть активные брони/выписки; подтвердите аннуляцию",
                details={
                    "services": [str(s.id) for s in active[:20]],
                    "hint": "Передайте confirm_cancellation=true",
                },
                status_code=409,
            )

        from services.models import OrderService

        cancelled_services = []
        with transaction.atomic():
            for service in active.select_for_update():
                service.status = OrderService.Status.CANCELLED
                service.updated_by = request.user
                service.version += 1
                service.save(update_fields=["status", "updated_by", "version", "updated_at"])
                cancelled_services.append(str(service.id))

            order = order_service.transition_order(
                order_id=order_id,
                user=request.user,
                target_status=Order.Status.CANCELLED,
                reason=str(request.data.get("reason", "")),
                expected_version=request.data.get("version"),
                request=request,
            )

        data = OrderDetailSerializer(order).data
        data["cancelled_services"] = cancelled_services
        data["audit_event_id"] = getattr(order, "_audit_event_id", None)
        return Response(data)


class OrderReassignView(APIView):
    permission_classes = [require("orders.reassign")]

    def post(self, request, order_id):
        from accounts.models import User

        get_order_or_404(request.user, order_id)
        new_operator = User.objects.filter(
            pk=request.data.get("operator"), tenant_id=request.user.tenant_id, status=User.Status.ACTIVE
        ).first()
        if new_operator is None:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Оператор не найден",
                fields={"operator": ["Активный пользователь обязателен"]},
                status_code=400,
            )
        order = order_service.reassign_order(
            order_id=order_id,
            user=request.user,
            new_operator=new_operator,
            reason=str(request.data.get("reason", "")),
            expected_version=request.data.get("version"),
            request=request,
        )
        return Response(OrderDetailSerializer(order).data)


class OrderParticipantsView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        participants = order.participants.filter(status="active").select_related("person")
        return Response(ParticipantSerializer(participants, many=True).data)

    def post(self, request, order_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        order = get_order_or_404(request.user, order_id)
        serializer = ParticipantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        participant = serializer.save(tenant_id=order.tenant_id, order=order, created_by=request.user)
        emit_event("order.updated", order, payload={"action": "participant_added"})
        audit("order.participant_added", actor=request.user, resource=order, request=request)
        return Response(ParticipantSerializer(participant).data, status=http.HTTP_201_CREATED)


class OrderParticipantDetailView(APIView):
    permission_classes = [require("orders.change")]

    def delete(self, request, order_id, participant_id):
        order = get_order_or_404(request.user, order_id)
        participant = order.participants.filter(pk=participant_id, status="active").first()
        if participant is None:
            raise ApiError(code="NOT_FOUND", message="Участник не найден", status_code=404)
        if participant.service_passengers.exclude(status__in=["cancelled", "replaced"]).exists():
            raise ApiError(
                code="PARTICIPANT_HAS_SERVICES",
                message="Участник привязан к услугам; сначала cancel/replace",
                status_code=409,
            )
        participant.status = "removed"
        participant.save(update_fields=["status"])
        audit("order.participant_removed", actor=request.user, resource=order, request=request)
        return Response(status=http.HTTP_204_NO_CONTENT)


class OrderRouteView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        route = getattr(order, "route", None)
        if route is None:
            raise ApiError(code="NOT_FOUND", message="Маршрут не задан", status_code=404)
        return Response(RouteSerializer(route).data)

    def patch(self, request, order_id):
        from orders.models import Route, RoutePoint

        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        order = get_order_or_404(request.user, order_id)
        with transaction.atomic():
            route = Route.objects.select_for_update().filter(order=order).first()
            if route is None:
                route = Route.objects.create(tenant_id=order.tenant_id, order=order, created_by=request.user)
            else:
                check_version(route, request.data.get("version"))
            if kind := request.data.get("kind"):
                route.kind = kind
            points = request.data.get("points")
            if points is not None:
                if len(points) < 2:
                    raise ApiError(
                        code="VALIDATION_ERROR",
                        message="Маршрут содержит минимум 2 точки",
                        fields={"points": ["Минимум 2 точки"]},
                        status_code=400,
                    )
                route.points.all().delete()
                for index, point in enumerate(points, start=1):
                    point_serializer = RoutePointSerializer(data=point)
                    point_serializer.is_valid(raise_exception=True)
                    RoutePoint.objects.create(
                        tenant_id=order.tenant_id,
                        route=route,
                        sequence=index,
                        created_by=request.user,
                        **point_serializer.validated_data,
                    )
            route.version += 1
            route.updated_by = request.user
            route.save()
            emit_event("order.updated", order, payload={"action": "route_changed"})
            audit("order.route_changed", actor=request.user, resource=order, request=request)
        return Response(RouteSerializer(route).data)


class OrderOverviewView(APIView):
    """Оптимизированный агрегат карточки (ТЗ §7.3, §25.2)."""

    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = (
            orders_visible_to(request.user)
            .select_related("client_person", "client_company", "operator", "contact_person")
            .prefetch_related("participants__person", "route__points", "services__passengers", "tasks")
            .filter(pk=order_id)
            .first()
        )
        if order is None:
            raise ApiError(code="NOT_FOUND", message="Заказ не найден", status_code=404)
        services_summary = [
            {
                "id": str(s.id),
                "kind": s.kind,
                "status": s.status,
                "title": s.title,
                "starts_at": s.starts_at,
                "client_total": str(s.client_total) if s.client_total else None,
                "currency": s.currency,
                "ticketing_deadline": s.ticketing_deadline,
                "version": s.version,
            }
            for s in order.services.all()
        ]
        route = getattr(order, "route", None)
        tasks = order.tasks.order_by("due_at", "created_at")
        history = OrderStatusHistory.objects.filter(order=order).select_related("changed_by")[:100]
        finance = order_finance_summary(order)
        from aftersales.models import AfterSaleCase
        from aftersales.views import CaseSerializer
        from documents.models import Document
        from documents.serializers import DocumentSerializer
        from offers.models import Proposal
        from offers.views import ProposalSerializer

        proposals_qs = (
            Proposal.objects.filter(tenant_id=request.user.tenant_id, order=order, archived_at__isnull=True)
            .prefetch_related("variants__items")
            .order_by("-created_at")
        )
        documents_qs = Document.objects.filter(
            tenant_id=request.user.tenant_id, order=order, archived_at__isnull=True
        ).order_by("-created_at")
        returns_qs = (
            AfterSaleCase.objects.filter(tenant_id=request.user.tenant_id, order=order, archived_at__isnull=True)
            .prefetch_related("participants", "quotes")
            .order_by("-created_at")
        )
        proposals = ProposalSerializer(proposals_qs, many=True).data
        documents = DocumentSerializer(documents_qs, many=True).data
        returns = CaseSerializer(returns_qs, many=True, context={"request": request}).data
        deadlines = [
            {"service_id": s["id"], "kind": "ticketing", "at": s["ticketing_deadline"]}
            for s in services_summary
            if s["ticketing_deadline"]
        ]
        warnings = []
        now = timezone.now()
        for deadline in deadlines:
            if deadline["at"] and deadline["at"] < now:
                warnings.append({"code": "DEADLINE_OVERDUE", "service_id": deadline["service_id"]})
        return Response(
            {
                "order": OrderDetailSerializer(order).data,
                "route": RouteSerializer(route).data if route else [],
                "participants": ParticipantSerializer(order.participants.filter(status="active").select_related("person"), many=True).data,
                "services": services_summary,
                "tasks": OrderTaskSerializer(tasks, many=True).data,
                "history": [
                    {
                        "id": h.id,
                        "from_status": h.from_status,
                        "to_status": h.to_status,
                        "from_stage": h.from_stage,
                        "to_stage": h.to_stage,
                        "reason": h.reason,
                        "changed_by": str(h.changed_by_id) if h.changed_by_id else None,
                        "changed_at": h.changed_at,
                    }
                    for h in history
                ],
                "finance": finance,
                "finance_summary": finance,
                "proposals": proposals,
                "documents": documents,
                "returns": returns,
                "allowed_actions": order_service.allowed_actions(order, request.user),
                "deadlines": deadlines,
                "warnings": warnings,
            }
        )


class OrderHistoryView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        qs = OrderStatusHistory.objects.filter(order=order).select_related("changed_by")
        if to_status := request.query_params.get("to_status"):
            qs = qs.filter(to_status=to_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(
            [
                {
                    "id": h.id,
                    "from_status": h.from_status,
                    "to_status": h.to_status,
                    "from_stage": h.from_stage,
                    "to_stage": h.to_stage,
                    "reason": h.reason,
                    "changed_by": str(h.changed_by_id) if h.changed_by_id else None,
                    "changed_at": h.changed_at,
                }
                for h in page
            ]
        )


class OrderTasksView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        qs = order.tasks.order_by("due_at")
        if task_status := request.query_params.get("status"):
            qs = qs.filter(status=task_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(OrderTaskSerializer(page, many=True).data)

    def post(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        serializer = OrderTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = serializer.save(tenant_id=order.tenant_id, order=order, created_by=request.user)
        emit_event("order.updated", order, payload={"action": "task_created"}, audience_user=task.assignee)
        return Response(OrderTaskSerializer(task).data, status=http.HTTP_201_CREATED)


class OrderAllowedActionsView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        return Response(order_service.allowed_actions(order, request.user))


class OrderDuplicateView(APIView):
    permission_classes = [require("orders.create")]

    @idempotent_command("orders.duplicate", required=False)
    def post(self, request, order_id):
        get_order_or_404(request.user, order_id)
        order = order_service.duplicate_order(order_id=order_id, user=request.user, request=request)
        return Response(OrderDetailSerializer(order).data, status=http.HTTP_201_CREATED)


class OrderFinanceSummaryView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        return Response(order_finance_summary(order))
