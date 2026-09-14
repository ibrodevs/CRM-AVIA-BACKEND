"""Постановка документа в очередь отправки клиенту.

Кнопка «Отправить клиенту» раньше возвращала `{"status": "queued"}`, не создавая
при этом никакой очереди: наружу не уходило ничего, а в аудите оставалась запись
«документ отправлен». Здесь отправка ставится в реальную очередь
(`common.OutboundDelivery`), получатель берётся из карточки клиента, а ответ
честно говорит, настроен ли канал.
"""

from __future__ import annotations

from common.models import OutboundDelivery
from common.transports import IN_APP_CHANNELS, is_configured, not_configured_reason

_KIND_TITLES = {
    "itinerary_receipt": "Маршрутная квитанция",
    "ticket": "Билет",
    "voucher": "Ваучер",
    "insurance_policy": "Страховой полис",
    "invoice": "Счёт",
    "act": "Акт",
    "contract": "Договор",
}


def resolve_recipient(document, channel: str, explicit: str = "") -> str:
    """Адрес клиента в канале: из запроса, карточки документа или заказа."""
    if explicit:
        return explicit.strip()
    if channel in IN_APP_CHANNELS:
        return "in-app"

    order = document.order
    person = document.person or (order.client_person if order else None)
    company = document.company or (order.client_company if order else None)

    if channel == "email":
        return (getattr(person, "email", "") or getattr(company, "email", "") or "").strip()
    if channel in ("sms", "whatsapp"):
        return (getattr(person, "phone", "") or getattr(company, "phone", "") or "").strip()
    if channel in ("telegram", "max"):
        # Мессенджер-идентификатор клиента хранится в переписке с ним.
        thread = document.order.chat_threads.filter(
            type="client", external_channel=channel
        ).first() if document.order_id else None
        return (thread.external_account if thread else "").strip()
    return ""


def queue_document_send(document, *, channel: str, recipient: str = "", requested_by=None) -> dict:
    """Ставит документ в очередь и возвращает описание состояния для интерфейса."""
    address = resolve_recipient(document, channel, recipient)
    subject = f"{_KIND_TITLES.get(document.kind, 'Документ')}: {document.title}"
    order_line = f"\nЗаказ: {document.order.number}" if document.order_id else ""
    body = (
        f"Здравствуйте!\n\nВо вложении — {subject.lower()}.{order_line}\n\n"
        "С уважением, служба поддержки."
    )

    delivery = OutboundDelivery.objects.create(
        tenant_id=document.tenant_id,
        resource_type="Document",
        resource_id=str(document.id),
        purpose="document",
        channel=channel,
        recipient=address,
        subject=subject[:255],
        body=body,
        document=document,
        requested_by=requested_by,
        created_by=requested_by,
    )

    configured = is_configured(channel)
    if not address and channel not in IN_APP_CHANNELS:
        detail = "Не удалось определить адрес клиента для этого канала"
    elif configured:
        detail = "Поставлено в очередь отправки"
    else:
        detail = not_configured_reason(channel)
    return {
        "status": "queued",
        "channel": channel,
        "delivery": str(delivery.id),
        "recipient": address,
        "channel_configured": configured,
        "detail": detail,
    }
