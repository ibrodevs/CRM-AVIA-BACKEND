"""Общий исполнитель очередей доставки.

Три очереди (`NotificationDelivery`, `OutboundMessageDelivery`, `OutboundDelivery`)
описаны одинаково: канал, получатель, состояние, число попыток, ошибка, время
отправки. Здесь — единственная реализация их конечного автомата, чтобы правила
повторов и формулировки ошибок не разъезжались между уведомлениями, чатом и
документами.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from common.transports import (
    Attachment,
    ChannelNotConfigured,
    PermanentTransportError,
    TransportError,
    is_configured,
    not_configured_reason,
    send_message,
)

logger = logging.getLogger("travelhub.delivery")

#: Состояния записи доставки.
QUEUED = "queued"
SENT = "sent"
FAILED = "failed"
SKIPPED = "skipped"


def max_attempts() -> int:
    return int(getattr(settings, "DELIVERY_MAX_ATTEMPTS", 5))


def batch_size() -> int:
    return int(getattr(settings, "DELIVERY_BATCH_SIZE", 100))


@dataclass
class DeliveryOutcome:
    state: str
    error: str = ""
    detail: str = ""
    external_id: str = ""

    @property
    def delivered(self) -> bool:
        return self.state == SENT


def attempt(
    record,
    *,
    recipient: str,
    subject: str = "",
    body: str = "",
    attachment: Attachment | None = None,
    extra_update_fields: tuple[str, ...] = (),
) -> DeliveryOutcome:
    """Одна попытка доставки. Обновляет запись и возвращает её новое состояние.

    Запись переходит в `skipped`, если канал не настроен: это не ошибка
    доставки, а отсутствие настройки — повторять такую попытку бесполезно, а
    показать оператору её надо иначе, чем сбой связи.
    """
    channel = record.channel
    update_fields = ["state", "attempts", "error", "sent_at", *extra_update_fields]
    record.attempts = (record.attempts or 0) + 1

    # Ненастроенный канал важнее отсутствующего адреса: адрес для него всё равно
    # негде было бы взять, а причина, которую увидит оператор, должна быть корневой.
    if not is_configured(channel):
        record.state = SKIPPED
        record.error = not_configured_reason(channel)[:255]
        record.save(update_fields=update_fields)
        return DeliveryOutcome(state=SKIPPED, error=record.error)

    if not recipient:
        record.state = FAILED
        record.error = "Не удалось определить адрес получателя для канала"[:255]
        record.sent_at = None
        record.save(update_fields=update_fields)
        return DeliveryOutcome(state=FAILED, error=record.error)

    try:
        result = send_message(channel, recipient, subject=subject, body=body, attachment=attachment)
    except ChannelNotConfigured as exc:
        record.state = SKIPPED
        record.error = str(exc)[:255]
        record.save(update_fields=update_fields)
        return DeliveryOutcome(state=SKIPPED, error=record.error)
    except PermanentTransportError as exc:
        record.state = FAILED
        record.error = str(exc)[:255]
        record.save(update_fields=update_fields)
        logger.warning("delivery rejected", extra={"channel": channel, "error": str(exc)[:200]})
        return DeliveryOutcome(state=FAILED, error=record.error)
    except TransportError as exc:
        exhausted = record.attempts >= max_attempts()
        record.state = FAILED if exhausted else QUEUED
        record.error = str(exc)[:255]
        record.save(update_fields=update_fields)
        logger.warning(
            "delivery attempt failed",
            extra={"channel": channel, "attempts": record.attempts, "error": str(exc)[:200]},
        )
        return DeliveryOutcome(state=record.state, error=record.error)

    record.state = SENT
    record.error = ""
    record.sent_at = timezone.now()
    record.save(update_fields=update_fields)
    return DeliveryOutcome(state=SENT, detail=result.detail, external_id=result.external_id)


def pending_filter(queryset):
    """Записи, которые ещё имеет смысл отправлять."""
    return queryset.filter(state=QUEUED, attempts__lt=max_attempts())


def attachment_from_document_version(version) -> Attachment | None:
    """Читает файл версии документа во вложение. None — если файла нет на диске."""
    if version is None or not version.file:
        return None
    try:
        with version.file.open("rb") as handle:
            content = handle.read()
    except (FileNotFoundError, OSError, ValueError):
        return None
    name = version.original_name or version.file.name.rsplit("/", 1)[-1]
    return Attachment(filename=name, content=content, mime_type=version.mime_type or "")
