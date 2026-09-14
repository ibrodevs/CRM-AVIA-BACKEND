"""Разбор очереди доставки уведомлений во внешние каналы.

Записи `NotificationDelivery` создавались с самого начала, но читать их было
некому: e-mail, Telegram, SMS не уходили никогда, при этом переключатели каналов
в профиле и настройках работали и создавали у пользователя уверенность, что
всё включено. Эта задача — недостающий исполнитель очереди.
"""

from __future__ import annotations

from common.delivery import SENT, SKIPPED, attempt, batch_size, pending_filter
from common.scheduled import scheduled_task
from common.transports import IN_APP_CHANNELS
from notifications.models import NotificationDelivery


def recipient_for(user, channel: str) -> str:
    """Адрес пользователя в канале. Пустая строка — адреса нет."""
    if channel in IN_APP_CHANNELS:
        return "in-app"
    if channel == "email":
        return user.email or ""
    if channel == "telegram":
        return (user.telegram or "").strip()
    if channel == "whatsapp":
        return (user.whatsapp or user.phone or "").strip()
    if channel == "max":
        return (user.max or "").strip()
    if channel == "sms":
        return (user.phone or user.work_phone or "").strip()
    return ""


@scheduled_task("notifications.dispatch_deliveries")
def dispatch_deliveries() -> str:
    """Отправляет уведомления, поставленные в очередь правилами."""
    queue = pending_filter(
        NotificationDelivery.objects.select_related("notification", "notification__user")
    ).order_by("id")[: batch_size()]

    sent = skipped = failed = 0
    for delivery in queue:
        notification = delivery.notification
        outcome = attempt(
            delivery,
            recipient=recipient_for(notification.user, delivery.channel),
            subject=notification.title,
            body=notification.body,
        )
        if outcome.state == SENT:
            sent += 1
        elif outcome.state == SKIPPED:
            skipped += 1
        else:
            failed += 1
    return f"sent {sent}, skipped {skipped}, failed {failed}"
