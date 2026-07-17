import fnmatch
from datetime import timedelta

from django.utils import timezone

from common.models import OutboxEvent
from common.outbox_processors import outbox_processor
from common.scheduled import scheduled_task
from notifications.models import DeadlineThreshold, Notification, NotificationDelivery, NotificationRule

_TITLES = {
    "order.updated": "Обновление заказа",
    "booking.updated": "Обновление бронирования",
    "ticketing.updated": "Обновление выписки",
    "search.completed": "Поиск завершён",
    "search.failed": "Поиск не удался",
    "chat.mention": "Вас упомянули в чате",
    "chat.message.created": "Новое сообщение",
    "notification.created": "Уведомление",
}


@outbox_processor("*")
def create_notifications(event: OutboxEvent) -> None:
    """Применяет NotificationRule к событию и создаёт персональные уведомления."""
    if event.tenant_id is None or event.event_type.startswith("notification."):
        return
    rules = NotificationRule.objects.filter(
        tenant_id=event.tenant_id, is_active=True, archived_at__isnull=True
    )
    for rule in rules:
        if not fnmatch.fnmatch(event.event_type, rule.event_type):
            continue
        for user in _resolve_recipients(rule, event):
            notification = Notification.objects.create(
                tenant_id=event.tenant_id,
                user=user,
                priority=rule.priority,
                source=event.event_type.split(".")[0],
                event_type=event.event_type,
                title=_TITLES.get(event.event_type, rule.name),
                body=str(event.payload)[:500],
                resource_type=event.resource_type,
                resource_id=event.resource_id,
            )
            for channel in rule.channels or ["desktop"]:
                NotificationDelivery.objects.create(notification=notification, channel=channel)
            from common.outbox import emit_event

            emit_event(
                "notification.created",
                notification,
                payload={"priority": rule.priority},
                audience_user=user,
                tenant_id=event.tenant_id,
            )


def _resolve_recipients(rule: NotificationRule, event: OutboxEvent):
    from accounts.models import User

    recipients = rule.recipients or {}
    users = set()
    if user_ids := recipients.get("users"):
        users.update(
            User.objects.filter(pk__in=user_ids, tenant_id=event.tenant_id, status=User.Status.ACTIVE)
        )
    if roles := recipients.get("roles"):
        users.update(
            User.objects.filter(
                user_roles__role__code__in=roles, tenant_id=event.tenant_id, status=User.Status.ACTIVE
            )
        )
    if event.audience_user_id:
        users.update(User.objects.filter(pk=event.audience_user_id))
    return users


@scheduled_task("notifications.check_deadlines")
def check_deadlines() -> str:
    """Дедлайны выписки/оплаты без дублей: unique threshold key (ТЗ §18)."""
    from services.models import OrderService

    now = timezone.now()
    created = 0
    for hours, threshold in ((24, "24h"), (2, "2h")):
        services = OrderService.objects.filter(
            ticketing_deadline__gt=now,
            ticketing_deadline__lte=now + timedelta(hours=hours),
            status__in=["booked", "confirmed"],
        ).select_related("order")
        for service in services:
            recipient = service.responsible or service.order.operator
            if recipient is None:
                continue
            _, was_created = DeadlineThreshold.objects.get_or_create(
                rule_key="ticketing_deadline",
                resource_type="OrderService",
                resource_id=str(service.pk),
                threshold=threshold,
                recipient=recipient,
            )
            if was_created:
                Notification.objects.create(
                    tenant_id=service.tenant_id,
                    user=recipient,
                    priority="critical" if threshold == "2h" else "high",
                    source="services",
                    event_type="service.deadline",
                    title=f"Дедлайн выписки через {threshold}",
                    body=f"{service.title}: выписка до {service.ticketing_deadline}",
                    resource_type="OrderService",
                    resource_id=str(service.pk),
                )
                created += 1

    overdue = OrderService.objects.filter(
        ticketing_deadline__lt=now,
        status__in=["booked", "confirmed"],
    ).select_related("order")
    for service in overdue:
        recipient = service.responsible or service.order.operator
        if recipient is None:
            continue
        _, was_created = DeadlineThreshold.objects.get_or_create(
            rule_key="ticketing_deadline",
            resource_type="OrderService",
            resource_id=str(service.pk),
            threshold="overdue",
            recipient=recipient,
        )
        if was_created:
            Notification.objects.create(
                tenant_id=service.tenant_id,
                user=recipient,
                priority="critical",
                source="services",
                event_type="service.deadline_overdue",
                title="Дедлайн выписки просрочен",
                body=f"{service.title}: дедлайн был {service.ticketing_deadline}",
                resource_type="OrderService",
                resource_id=str(service.pk),
            )
            created += 1
    return f"created {created} deadline notifications"
