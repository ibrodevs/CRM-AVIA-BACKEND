"""Текст уведомления на русском языке.

Раньше в тело уведомления клали `str(event.payload)` — Python-дамп словаря.
Фронтенд научился разбирать такой дамп в фразу, но это лечило симптом: любой
другой потребитель (письмо, Telegram, SMS) получил бы мусор. Текст формируется
здесь, один раз, при создании уведомления — и уходит во все каналы одинаковым.
"""

from __future__ import annotations

TITLES = {
    "order.updated": "Обновление заказа",
    "booking.updated": "Обновление бронирования",
    "ticketing.updated": "Обновление выписки",
    "service.updated": "Обновление услуги",
    "search.completed": "Поиск завершён",
    "search.failed": "Поиск не удался",
    "chat.mention": "Вас упомянули в чате",
    "chat.message.created": "Новое сообщение",
    "notification.created": "Уведомление",
    "sla.breached": "Просрочен норматив SLA",
    "offers.updated": "Обновление подборки",
    "calendar.event.updated": "Изменение в календаре",
    "workspace.updated": "Изменение настроек рабочего пространства",
    "users.invited": "Приглашён сотрудник",
}

_ACTIONS = {
    "created": "создан",
    "updated": "обновлён",
    "deleted": "удалён",
    "cancelled": "отменён",
    "confirmed": "подтверждён",
    "status_changed": "сменил статус",
    "payment_confirmed": "платёж подтверждён",
    "refund_executed": "возврат проведён",
    "book": "бронирование",
    "manual_booked": "бронирование вручную",
    "issue": "выписка",
    "manual_issued": "выписка вручную",
    "exchange": "обмен",
    "refund": "возврат",
    "cancellation": "отмена",
    "aftersale_status": "изменение по обращению",
    "document_generated": "документ сформирован",
    "document_sent": "документ отправлен клиенту",
    "breached": "норматив нарушен",
}

_STATUSES = {
    "new": "Новое",
    "in_progress": "В работе",
    "awaiting_confirmation": "Ожидает подтверждения",
    "awaiting_payment": "Ожидание оплаты",
    "paid": "Оплачено",
    "completed": "Завершено",
    "needs_review": "Требует проверки",
    "on_hold": "На паузе",
    "data_missing": "Нет данных",
    "cancelled": "Отменено",
    "searching": "Подбор",
    "proposed": "Предложено",
    "approval": "На согласовании",
    "booked": "Забронировано",
    "confirmed": "Подтверждено",
    "issued": "Выписано",
    "refund_in_progress": "Возврат в процессе",
    "refunded": "Возвращено",
    "failed": "Ошибка",
}

_KINDS = {
    "avia": "Авиа",
    "rail": "ЖД",
    "hotel": "Гостиница",
    "transfer": "Трансфер",
    "visa": "Виза",
    "insurance": "Страхование",
    "tour": "Тур",
    "other": "Прочее",
}

_FIELDS = (
    ("channel", "Канал"),
    ("reason", "Причина"),
    ("comment", "Комментарий"),
    ("error_code", "Код ошибки"),
    ("message", "Сообщение"),
    ("supplier", "Поставщик"),
    ("amount", "Сумма"),
    ("currency", "Валюта"),
)


def _status(value) -> str:
    return _STATUSES.get(str(value), str(value))


def _resource_label(event) -> str:
    """Человеческое имя ресурса: номер заказа, название услуги, номер документа."""
    resource_type, resource_id = event.resource_type, event.resource_id
    if not resource_id:
        return ""
    try:
        if resource_type == "Order":
            from orders.models import Order

            order = Order.objects.filter(pk=resource_id).first()
            return f"Заказ {order.number}" if order else ""
        if resource_type == "OrderService":
            from services.models import OrderService

            service = OrderService.objects.filter(pk=resource_id).select_related("order").first()
            if service is None:
                return ""
            kind = _KINDS.get(service.kind, service.kind)
            return f"{kind}: {service.title} (заказ {service.order.number})"
        if resource_type == "Document":
            from documents.models import Document

            document = Document.objects.filter(pk=resource_id).first()
            return f"Документ «{document.title}»" if document else ""
        if resource_type == "Message":
            from communications.models import Message

            message = Message.objects.filter(pk=resource_id).select_related("thread").first()
            if message is None:
                return ""
            return message.thread.title or "Чат"
        if resource_type == "SlaInstance":
            from workforce.models import SlaInstance

            instance = SlaInstance.objects.filter(pk=resource_id).first()
            return _sla_label(instance) if instance else ""
    except Exception:  # noqa: BLE001 — текст уведомления не должен ронять обработку события
        return ""
    return ""


def _sla_label(instance) -> str:
    if instance.resource_type.lower() in ("order", "orders.order"):
        from orders.models import Order

        order = Order.objects.filter(pk=instance.resource_id).first()
        if order:
            return f"Заказ {order.number}"
    if instance.resource_type in ("OrderService", "orderservice"):
        from services.models import OrderService

        service = OrderService.objects.filter(pk=instance.resource_id).first()
        if service:
            return service.title
    return f"{instance.resource_type} {instance.resource_id}"


def render_title(event, rule_name: str = "") -> str:
    return TITLES.get(event.event_type, rule_name or "Уведомление")


def render_body(event) -> str:
    """Короткая фраза о том, что произошло. Без словарей и служебных id."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    parts: list[str] = []

    if label := _resource_label(event):
        parts.append(label)

    action = str(payload.get("action", ""))
    if action:
        parts.append(_ACTIONS.get(action, action))

    if "to" in payload or "to_status" in payload:
        target = payload.get("to", payload.get("to_status"))
        source = payload.get("from", payload.get("from_status"))
        if source:
            parts.append(f"статус: {_status(source)} → {_status(target)}")
        else:
            parts.append(f"статус: {_status(target)}")
    elif "status" in payload:
        parts.append(f"статус: {_status(payload['status'])}")

    for key, label in _FIELDS:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        parts.append(f"{label}: {value}")

    if not parts:
        parts.append(TITLES.get(event.event_type, event.event_type))
    return " · ".join(str(part) for part in parts)[:500]
