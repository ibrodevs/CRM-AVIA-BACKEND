"""Разбор очереди отправок клиенту: документы, акты сверки, выгрузки."""

from __future__ import annotations

from common.delivery import (
    SENT,
    SKIPPED,
    attachment_from_document_version,
    attempt,
    batch_size,
    pending_filter,
)
from common.models import OutboundDelivery
from common.scheduled import scheduled_task


def _attachment_for(delivery: OutboundDelivery):
    """Вложение отправки: последняя версия документа либо файл из payload."""
    if delivery.document_id:
        version = delivery.document.versions.order_by("-version").first()
        return attachment_from_document_version(version)
    inline = delivery.payload.get("inline_attachment")
    if inline:
        from common.transports import Attachment

        return Attachment(
            filename=inline.get("filename", "document.txt"),
            content=str(inline.get("text", "")).encode("utf-8"),
            mime_type=inline.get("content_type", "text/plain; charset=utf-8"),
        )
    return None


@scheduled_task("common.dispatch_outbound_deliveries")
def dispatch_outbound_deliveries() -> str:
    queue = pending_filter(
        OutboundDelivery.all_objects.select_related("document")
    ).order_by("created_at")[: batch_size()]

    sent = skipped = failed = 0
    for delivery in queue:
        outcome = attempt(
            delivery,
            recipient=delivery.recipient,
            subject=delivery.subject,
            body=delivery.body,
            attachment=_attachment_for(delivery),
        )
        if outcome.state == SENT:
            sent += 1
        elif outcome.state == SKIPPED:
            skipped += 1
        else:
            failed += 1
    return f"sent {sent}, skipped {skipped}, failed {failed}"
