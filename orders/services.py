"""Application service заказов: создание, статусы, переназначение (ТЗ §7).

Создание заказа из реестра, календаря и подбора использует один и тот же
create_order() — отдельной упрощённой логики нумерации нет (ТЗ §7.4).
"""
from django.db import transaction
from django.utils import timezone

from accounts.permissions import has_permission
from common.audit import audit
from common.errors import ApiError, BusinessRejectionError, TransitionForbiddenError
from common.locking import check_version
from common.outbox import emit_event
from crm.models import Agreement
from orders.models import (
    ORDER_TRANSITIONS, TERMINAL_ORDER_STATUSES, Order, OrderParticipant,
    OrderReassignment, OrderStatusHistory, Route, RoutePoint, allocate_order_number,
)


def _record_status(order: Order, from_status: str, from_stage: str, *, reason: str, user) -> None:
    OrderStatusHistory.objects.create(
        order=order, from_status=from_status, to_status=order.status,
        from_stage=from_stage, to_stage=order.stage, reason=reason, changed_by=user,
    )


def _active_agreement_snapshot(agreement: Agreement | None) -> dict | None:
    if agreement is None:
        return None
    return {
        "agreement_id": str(agreement.id),
        "number": agreement.number,
        "version": agreement.agreement_version,
        "effective_from": str(agreement.effective_from) if agreement.effective_from else None,
        "effective_to": str(agreement.effective_to) if agreement.effective_to else None,
        "fee_rules": [
            {
                "service_kind": r.service_kind, "fee_kind": r.fee_kind,
                "calculation": r.calculation, "value": str(r.value), "currency": r.currency,
            }
            for r in agreement.fee_rules.all()
        ],
        "snapshotted_at": timezone.now().isoformat(),
    }


@transaction.atomic
def create_order(*, tenant_id, user, data: dict, request=None) -> Order:
    """Атомарно создаёт заказ, маршрут, участников и договорный snapshot."""
    agreement = data.get("agreement")
    order = Order(
        tenant_id=tenant_id,
        number=allocate_order_number(tenant_id),
        request_type=data.get("request_type", Order.RequestType.INDIVIDUAL),
        client_person=data.get("client_person"),
        client_company=data.get("client_company"),
        contact_person=data.get("contact_person"),
        priority=data.get("priority", Order.Priority.NORMAL),
        operator=data.get("operator") or user,
        source=data.get("source", ""),
        preferred_channel=data.get("preferred_channel", ""),
        base_currency=data.get("base_currency", "USD"),
        agreement=agreement,
        agreement_snapshot=_active_agreement_snapshot(agreement),
        planned_start=data.get("planned_start"),
        planned_end=data.get("planned_end"),
        purpose=data.get("purpose", ""),
        comment=data.get("comment", ""),
        is_group=data.get("request_type") == Order.RequestType.GROUP,
        created_by=user,
        updated_by=user,
    )
    order.save()

    route_data = data.get("route")
    if route_data:
        route = Route.objects.create(tenant_id=tenant_id, order=order,
                                     kind=route_data.get("kind", Route.Kind.ONE_WAY),
                                     created_by=user)
        for index, point in enumerate(route_data.get("points", []), start=1):
            RoutePoint.objects.create(
                tenant_id=tenant_id, route=route, sequence=index,
                location_code=point["location_code"],
                location_type=point.get("location_type", "city"),
                location_name=point.get("location_name", ""),
                local_datetime=point.get("local_datetime"),
                timezone=point.get("timezone", ""),
                created_by=user,
            )

    for participant in data.get("participants", []):
        OrderParticipant.objects.create(
            tenant_id=tenant_id, order=order,
            person=participant.get("person"),
            guest_snapshot=participant.get("guest_snapshot"),
            role=participant.get("role", OrderParticipant.Role.PASSENGER),
            is_contact=participant.get("is_contact", False),
            created_by=user,
        )

    _record_status(order, "", "", reason="Заказ создан", user=user)
    emit_event("order.updated", order, payload={"action": "created", "number": order.number})
    audit("order.created", actor=user, resource=order, request=request,
          after={"number": order.number, "request_type": order.request_type})
    return order


@transaction.atomic
def transition_order(*, order_id, user, target_status: str, reason: str = "",
                     expected_version=None, request=None) -> Order:
    """Смена статуса заказа с блокировкой строки и проверкой машины состояний."""
    order = Order.objects.select_for_update().get(pk=order_id)
    check_version(order, expected_version)

    if target_status not in Order.Status.values:
        raise ApiError(code="UNKNOWN_STATUS", message=f"Неизвестный статус: {target_status}",
                       status_code=400)

    current = order.status
    allowed = ORDER_TRANSITIONS.get(current, set())

    # Откат терминального статуса — только администратор с причиной (ТЗ §7.2).
    if current in TERMINAL_ORDER_STATUSES:
        if not has_permission(user, "settings.manage"):
            raise TransitionForbiddenError(
                code="ORDER_STATUS_TRANSITION_FORBIDDEN",
                message=f"Переход из {current} запрещён",
                details={"current_status": current, "allowed": sorted(allowed)},
            )
        if not reason:
            raise ApiError(code="REASON_REQUIRED",
                           message="Откат терминального статуса требует причины", status_code=400)
    elif target_status not in allowed:
        raise TransitionForbiddenError(
            code="ORDER_STATUS_TRANSITION_FORBIDDEN",
            message=f"Переход из {current} в {target_status} запрещён",
            details={"current_status": current, "allowed": sorted(allowed)},
        )

    # paid рассчитывается из финансов; ручной перевод требует права (ТЗ §7.2).
    if target_status == Order.Status.PAID and not has_permission(user, "finance.approve_payment"):
        raise BusinessRejectionError(
            code="MANUAL_PAID_FORBIDDEN",
            message="Статус «оплачен» выставляется по финансовым данным; ручное "
                    "подтверждение требует права finance.approve_payment",
        )

    if target_status == Order.Status.COMPLETED:
        _check_completion_allowed(order)

    from_status, from_stage = order.status, order.stage
    order.status = target_status
    if target_status == Order.Status.COMPLETED:
        order.stage = Order.Stage.COMPLETED
    if target_status == Order.Status.CANCELLED:
        order.cancelled_at = timezone.now()
        order.cancelled_reason = reason
    order.version += 1
    order.updated_by = user
    order.save()

    _record_status(order, from_status, from_stage, reason=reason, user=user)
    emit_event("order.updated", order,
               payload={"action": "status_changed", "from": from_status, "to": target_status})
    event = audit("order.status_changed", actor=user, resource=order, request=request,
                  reason=reason, before={"status": from_status}, after={"status": target_status})
    order._audit_event_id = str(event.id)
    return order


def _check_completion_allowed(order: Order) -> None:
    """completed допустим, когда обязательные услуги терминальны и долга нет (ТЗ §7.2)."""
    from services.models import OrderService

    non_terminal = order.services.exclude(
        status__in=[OrderService.Status.ISSUED, OrderService.Status.REFUNDED,
                    OrderService.Status.CANCELLED, OrderService.Status.CONFIRMED]
    ).exclude(archived_at__isnull=False)
    if non_terminal.exists():
        raise BusinessRejectionError(
            code="ORDER_HAS_ACTIVE_SERVICES",
            message="Заказ нельзя завершить: есть незавершённые услуги",
            details={"services": [str(s.id) for s in non_terminal[:20]]},
        )
    # Проверка долга — по ledger (Этап 5); при отсутствии обязательств долг 0.
    try:
        from finance.models import FinancialObligation

        outstanding = FinancialObligation.objects.filter(
            order=order, status__in=["open", "partial"],
            direction="client_receivable",
        ).exists()
        if outstanding:
            raise BusinessRejectionError(
                code="ORDER_HAS_DEBT",
                message="Заказ нельзя завершить: есть непогашенные обязательства клиента",
            )
    except ImportError:
        pass


@transaction.atomic
def reassign_order(*, order_id, user, new_operator, reason: str = "",
                   expected_version=None, request=None) -> Order:
    order = Order.objects.select_for_update().get(pk=order_id)
    check_version(order, expected_version)
    previous = order.operator
    order.operator = new_operator
    order.version += 1
    order.updated_by = user
    order.save(update_fields=["operator", "version", "updated_by", "updated_at"])
    OrderReassignment.objects.create(
        order=order, previous_operator=previous, new_operator=new_operator,
        reason=reason, reassigned_by=user,
    )
    emit_event("order.updated", order, payload={"action": "reassigned"})
    audit("order.reassigned", actor=user, resource=order, request=request, reason=reason,
          before={"operator": str(previous.pk) if previous else None},
          after={"operator": str(new_operator.pk)})
    return order


@transaction.atomic
def duplicate_order(*, order_id, user, request=None) -> Order:
    """Новый заказ без provider identifiers и платежей (ТЗ §7.3)."""
    source = Order.objects.select_related("client_person", "client_company").get(pk=order_id)
    new_order = create_order(
        tenant_id=source.tenant_id, user=user, request=request,
        data={
            "request_type": source.request_type,
            "client_person": source.client_person,
            "client_company": source.client_company,
            "contact_person": source.contact_person,
            "priority": source.priority,
            "source": "duplicate",
            "preferred_channel": source.preferred_channel,
            "base_currency": source.base_currency,
            "agreement": source.agreement,
            "planned_start": source.planned_start,
            "planned_end": source.planned_end,
            "purpose": source.purpose,
            "comment": f"Дубликат заказа {source.number}",
            "participants": [
                {"person": p.person, "guest_snapshot": p.guest_snapshot, "role": p.role,
                 "is_contact": p.is_contact}
                for p in source.participants.filter(status="active")
            ],
            "route": _route_as_dict(source),
        },
    )
    return new_order


def _route_as_dict(order: Order) -> dict | None:
    route = getattr(order, "route", None)
    if route is None:
        return None
    return {
        "kind": route.kind,
        "points": [
            {"location_code": p.location_code, "location_type": p.location_type,
             "location_name": p.location_name, "local_datetime": p.local_datetime,
             "timezone": p.timezone}
            for p in route.points.all()
        ],
    }


def allowed_actions(order: Order, user) -> dict:
    """Действия с учётом прав и состояния (ТЗ §7.3)."""
    transitions = sorted(ORDER_TRANSITIONS.get(order.status, set()))
    can_change_status = has_permission(user, "orders.change_status")
    return {
        "transitions": transitions if can_change_status else [],
        "can_edit": has_permission(user, "orders.change")
        and order.status not in TERMINAL_ORDER_STATUSES,
        "can_reassign": has_permission(user, "orders.reassign"),
        "can_cancel": can_change_status
        and Order.Status.CANCELLED in ORDER_TRANSITIONS.get(order.status, set()),
        "can_duplicate": has_permission(user, "orders.create"),
        "can_add_services": has_permission(user, "services.search")
        and order.status not in TERMINAL_ORDER_STATUSES,
    }
