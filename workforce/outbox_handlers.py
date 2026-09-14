"""Запуск и закрытие отсчёта SLA по доменным событиям.

`SlaPolicy` и `SlaInstance` существовали, но экземпляры создавались только в
демо-данных: на реальном заказе отсчёт не начинался, поэтому и нарушать было
нечего. Здесь политика применяется к событию, а первое действие ответственного
закрывает отсчёт (`responded_at`).
"""

from __future__ import annotations

import fnmatch
from datetime import timedelta

from django.utils import timezone

from common.models import OutboxEvent
from common.outbox_processors import outbox_processor
from workforce.models import SlaInstance, SlaPolicy

#: Событие → ресурс, по которому ведётся отсчёт.
_TRACKED_RESOURCES = ("Order", "OrderService")

#: Действия, которые считаются реакцией ответственного на обращение.
_RESPONSE_ACTIONS = {
    "status_changed",
    "assigned",
    "booked",
    "book",
    "issue",
    "manual_booked",
    "manual_issued",
    "responded",
    "updated",
}


def _event_keys(event: OutboxEvent) -> list[str]:
    """Ключи, по которым подбирается политика: тип события и «тип.действие»."""
    action = str(event.payload.get("action", "")) if isinstance(event.payload, dict) else ""
    keys = [event.event_type]
    if action:
        base = event.event_type.rsplit(".", 1)[0]
        keys.append(f"{base}.{action}")
        keys.append(f"{event.event_type}.{action}")
    return keys


def _matching_policies(event: OutboxEvent, service_kind: str) -> list[SlaPolicy]:
    policies = SlaPolicy.objects.filter(tenant_id=event.tenant_id, archived_at__isnull=True)
    keys = _event_keys(event)
    matched = []
    for policy in policies:
        if not any(fnmatch.fnmatch(key, policy.event_type) for key in keys):
            continue
        if policy.service_kind and policy.service_kind != service_kind:
            continue
        matched.append(policy)
    return matched


def _assignee_and_kind(event: OutboxEvent):
    """Ответственный и вид услуги для ресурса события."""
    if event.resource_type == "Order":
        from orders.models import Order

        order = Order.objects.filter(pk=event.resource_id).first()
        return (order.operator if order else None), "", order
    if event.resource_type == "OrderService":
        from services.models import OrderService

        service = (
            OrderService.objects.filter(pk=event.resource_id).select_related("order").first()
        )
        if service is None:
            return None, "", None
        return (service.responsible or service.order.operator), service.kind, service
    return None, "", None


@outbox_processor("*")
def track_sla(event: OutboxEvent) -> None:
    if event.tenant_id is None or event.resource_type not in _TRACKED_RESOURCES:
        return
    if event.event_type.startswith(("sla.", "notification.")):
        return

    assignee, service_kind, resource = _assignee_and_kind(event)
    if resource is None:
        return

    action = str(event.payload.get("action", "")) if isinstance(event.payload, dict) else ""
    started_at = event.occurred_at or timezone.now()

    for policy in _matching_policies(event, service_kind):
        instance, created = SlaInstance.objects.get_or_create(
            tenant_id=event.tenant_id,
            policy=policy,
            resource_type=event.resource_type,
            resource_id=str(event.resource_id),
            defaults={
                "assignee": assignee,
                "started_at": started_at,
                "response_deadline": started_at + timedelta(minutes=policy.response_minutes),
                "resolution_deadline": (
                    started_at + timedelta(minutes=policy.resolution_minutes)
                    if policy.resolution_minutes
                    else None
                ),
            },
        )
        if created:
            # События о начале отсчёта не публикуем: правило уведомлений
            # настроено на паттерн `sla.*` и рассылается всем ролям — каждый
            # старт SLA превратился бы в уведомление для всего офиса. Открытый
            # отсчёт и так виден в очереди SLA; наружу сообщать нужно только
            # о нарушении.
            continue

        # Любое последующее содержательное действие закрывает отсчёт реакции.
        if instance.responded_at is None and action in _RESPONSE_ACTIONS:
            instance.responded_at = timezone.now()
            instance.save(update_fields=["responded_at", "updated_at"])
