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
from common.errors import ApiError
from common.models import OutboundDelivery
from common.scheduled import scheduled_task


def _attachment_for(delivery: OutboundDelivery):
    """Вложение отправки: последняя версия документа либо файл из payload."""
    if delivery.document_id:
        from documents.hotel_certificate import validate_certificate_document

        validate_certificate_document(delivery.document)
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
        try:
            attachment = _attachment_for(delivery)
        except ApiError as exc:
            delivery.state = OutboundDelivery.State.FAILED
            delivery.error = str(exc)[:255]
            delivery.save(update_fields=["state", "error"])
            failed += 1
            continue
        if delivery.document_id and delivery.document.service_id and delivery.document.service.kind == "hotel":
            from common.client_hotel import client_hotel_text

            private_ids = [delivery.document.order.number] if delivery.document.order_id else []
            delivery.body = client_hotel_text(delivery.body, private_ids)
            delivery.subject = client_hotel_text(delivery.subject, private_ids)
        outcome = attempt(
            delivery,
            recipient=delivery.recipient,
            subject=delivery.subject,
            body=delivery.body,
            attachment=attachment,
        )
        if outcome.state == SENT:
            sent += 1
        elif outcome.state == SKIPPED:
            skipped += 1
        else:
            failed += 1
    return f"sent {sent}, skipped {skipped}, failed {failed}"
