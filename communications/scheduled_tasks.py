"""Разбор очереди исходящих сообщений клиенту.

Оператор писал клиенту в Telegram/WhatsApp из CRM, сообщение отображалось как
отправленное, но наружу не уходило — запись `OutboundMessageDelivery` никто не
читал. Здесь она обрабатывается, а `Message.delivery_state` начинает отражать
реальное положение дел.
"""

from __future__ import annotations

from common.delivery import (
    FAILED,
    SENT,
    SKIPPED,
    attachment_from_document_version,
    attempt,
    batch_size,
    pending_filter,
)
from common.scheduled import scheduled_task
from communications.models import Message, OutboundMessageDelivery


def _recipient(delivery) -> str:
    """Явно заданный адрес доставки либо внешний аккаунт треда."""
    return (delivery.recipient or delivery.message.thread.external_account or "").strip()


def _subject(message) -> str:
    thread = message.thread
    return thread.title or (f"Заказ {thread.order.number}" if thread.order_id else "Сообщение от менеджера")


@scheduled_task("communications.dispatch_outbound")
def dispatch_outbound() -> str:
    queue = pending_filter(
        OutboundMessageDelivery.objects.select_related(
            "message", "message__thread", "message__thread__order", "message__attachment"
        )
    ).order_by("id")[: batch_size()]

    sent = skipped = failed = 0
    for delivery in queue:
        message = delivery.message
        outcome = attempt(
            delivery,
            recipient=_recipient(delivery),
            subject=_subject(message),
            body=message.body,
            attachment=attachment_from_document_version(message.attachment),
        )
        if outcome.state == SENT:
            state = Message.DeliveryState.SENT
            sent += 1
        elif outcome.state == SKIPPED:
            # Канал не настроен: сообщение осталось в CRM и клиенту не ушло.
            state = Message.DeliveryState.FAILED
            skipped += 1
        elif outcome.state == FAILED:
            state = Message.DeliveryState.FAILED
            failed += 1
        else:
            state = Message.DeliveryState.QUEUED
        if message.delivery_state != state:
            message.delivery_state = state
            message.save(update_fields=["delivery_state"])
    return f"sent {sent}, skipped {skipped}, failed {failed}"
