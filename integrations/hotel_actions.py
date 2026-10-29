"""Hotel operation capabilities declared by adapters, without remote requests."""

MANUAL_ACTIONS = (
    "change_dates",
    "change_room",
    "change_guests",
    "change_meal",
    "early_checkin",
    "late_checkout",
    "extra_bed",
    "special_requests",
    "contact_supplier",
    "request_free_cancel",
)


def hotel_capabilities():
    result = {
        key: {"mode": "manual", "reason": "Запрос оператору; бронь автоматически не изменяется."}
        for key in MANUAL_ACTIONS
    }
    result.update(
        {
            "refresh": {"mode": "unsupported", "reason": "У адаптера нет API проверки этой брони."},
            "cancel_booking": {"mode": "manual", "reason": "Отмена согласуется оператором с поставщиком."},
            "cancel_rooms": {
                "mode": "unsupported",
                "reason": "Поставщик не поддерживает безопасную отмену отдельных номеров.",
            },
            "remove_room": {
                "mode": "unsupported",
                "reason": "Удаление номера требует подтверждённой отмены у поставщика.",
            },
            "add_room": {
                "mode": "new_booking",
                "reason": "Нужно новое бронирование. Существующая бронь не изменяется.",
            },
            "documents": {"mode": "local"},
            "check_policy": {"mode": "local"},
            "cancel_free": {
                "mode": "unsupported",
                "reason": "Нет зафиксированного расчёта нулевого штрафа. Можно создать запрос на отмену без штрафа.",
            },
        }
    )
    return result


def api_hotel_capabilities(service, booking_key):
    result = hotel_capabilities()
    booking = (service.provider_snapshot or {}).get(booking_key) or {}
    if service.source == "api" and booking.get("locator"):
        result["refresh"] = {"mode": "api"}
        result["cancel_booking"] = {
            "mode": "api",
            "reason": "Отмена всей брони. Частичная отмена номеров недоступна.",
        }
        if service.status in ("cancelled", "refunded"):
            result["cancel_booking"] = {"mode": "unsupported", "reason": "Бронь уже отменена."}
        elif (
            booking.get("cancel_requested_ids")
            or booking.get("cancel_state") in ("sending", "requested", "unknown", "complete")
            or booking.get("state") == "cancelling"
        ):
            result["cancel_booking"] = {
                "mode": "unsupported",
                "reason": "Отмена уже отправлена. Нужна проверка статуса, повторная отмена запрещена.",
            }
        elif service.status not in ("booked", "confirmed", "issued"):
            result["cancel_booking"] = {
                "mode": "unsupported",
                "reason": "Сначала проверьте фактический статус брони у поставщика.",
            }
        if booking.get("order_id"):
            from services.models import OrderService

            shared = (
                OrderService.objects.filter(
                    tenant_id=service.tenant_id,
                    supplier_id=service.supplier_id,
                    kind="hotel",
                    archived_at__isnull=True,
                    **{f"provider_snapshot__{booking_key}__order_id": booking["order_id"]},
                )
                .exclude(pk=service.pk)
                .exists()
            )
            if shared:
                result["cancel_booking"] = {
                    "mode": "unsupported",
                    "reason": "Бронь поставщика связана с несколькими CRM-услугами. Требуется ручная сверка всей брони.",
                }
    return result
