"""Read-only projection of supplier snapshots; supplier IDs stay supplier-scoped."""

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation

from accounts.permissions import has_permission, has_service_action
from integrations.adapters import AdapterError, adapter_for_service
from integrations.hotel_actions import hotel_capabilities
from orders.contacts import contact_person_name


def booking_data(service):
    snapshot = service.provider_snapshot or {}
    return snapshot.get(str(snapshot.get("provider_adapter", "")) + "_booking") or {}


def capabilities(service):
    try:
        return adapter_for_service(service).hotel_action_capabilities(service)
    except AdapterError:
        result = hotel_capabilities()
        result["refresh"]["reason"] = "Адаптер поставщика не настроен."
        return result


def financial_data(service):
    # Policies are not a binding penalty quote. Unknown never means zero.
    reported_penalty = None
    snapshot = service.provider_snapshot or {}
    items = (snapshot.get("aanda_booking") or {}).get("items") or []
    if items and all(item.get("penalty") is not None for item in items):
        try:
            total = sum((Decimal(str(item["penalty"])) for item in items), Decimal(0))
            if total.is_finite() and total >= 0:
                reported_penalty = {
                    "amount": str(total),
                    "currency": (snapshot.get("price") or {}).get("currency", service.currency),
                }
        except (ValueError, TypeError, InvalidOperation):
            pass
    return {
        "supplier_cost": str(service.supplier_cost) if service.supplier_cost is not None else None,
        "client_cost": str(service.client_total) if service.client_total is not None else None,
        "service_fee": str(service.agency_fee) if service.agency_fee is not None else None,
        "penalty": None,
        "refund": None,
        "currency": service.currency,
        "penalty_exact": False,
        "reported_penalty": reported_penalty,
        "warning": "API не предоставляет зафиксированный расчёт штрафа и возврата для этой операции. Отсутствие суммы не означает отмену без штрафа.",
    }


def booking_summary(service):
    snapshot = service.provider_snapshot or {}
    hb = snapshot.get("hotelbook") or {}
    aa = snapshot.get("aanda") or {}
    offer = hb.get("offer") or {}
    itinerary = snapshot.get("itinerary") or {}
    booking = booking_data(service)
    details = (hb.get("hotel") or {}).get("details") or {}
    start = (
        offer.get("checkIn")
        or itinerary.get("check_in")
        or (service.starts_at.date().isoformat() if service.starts_at else "")
    )
    end = (
        offer.get("checkOut")
        or itinerary.get("check_out")
        or (service.ends_at.date().isoformat() if service.ends_at else "")
    )
    policy = (
        service.cancellation_rules
        or offer.get("finePolicies")
        or (snapshot.get("fare") or {}).get("cancellation_rules")
        or {}
    )
    source_rooms = (
        offer.get("rooms")
        or aa.get("rooms")
        or (snapshot.get(str(snapshot.get("provider_adapter", ""))) or {}).get("rooms")
        or itinerary.get("rooms")
        or []
    )
    source_rooms = source_rooms if isinstance(source_rooms, list) else []
    items = booking.get("items") or []
    passengers = list(
        service.passengers.filter(status="active", participant__status="active").select_related(
            "participant__person"
        )
    )
    guests = [
        {
            "participant": str(p.participant_id),
            "room_ref": p.room_ref,
            "name": contact_person_name(p.participant.person or p.participant.guest_snapshot or {}),
        }
        for p in passengers
    ]
    rooms = []
    for room in source_rooms:
        if not isinstance(room, dict):
            continue
        quantity = room.get("quantity", 1)
        quantity = quantity if isinstance(quantity, int) and 0 < quantity <= 100 else 1
        for _ in range(quantity):
            ref = f"room:{len(rooms)}"
            # One supplier item may contain several rooms. Never equate item and room.
            matched = [
                item
                for item in items
                if any(
                    isinstance(r, dict) and room.get("hash") and r.get("hash") == room["hash"]
                    for r in item.get("rooms", [])
                )
                or (
                    room.get("product_code")
                    and (item.get("hotel_code"), item.get("product_code"), item.get("rate_id"))
                    == (room.get("hotel_code"), room.get("product_code"), room.get("rate_id"))
                )
            ]
            item = matched[0] if len(matched) == 1 else {}
            meal = room.get("meal") or offer.get("meal") or {}
            meal_text = (
                (meal.get("description") or meal.get("TypeName") or meal.get("name") or "")
                if isinstance(meal, dict)
                else str(meal)
            )
            price = room.get("price")
            if price is None and room.get("total_price") is not None:
                currency = room.get("currency") or {}
                price = {
                    "amount": room["total_price"],
                    "currency": currency.get("name") if isinstance(currency, dict) else currency,
                }
            rooms.append(
                {
                    "ref": ref,
                    "provider_room_id": room.get("providerRoomId") or room.get("product_code"),
                    "supplier_item_id": item.get("itemId") or item.get("id"),
                    "category": room.get("roomName")
                    or room.get("room_name")
                    or itinerary.get("room")
                    or "Категория не указана",
                    "check_in": room.get("check_in") or start,
                    "check_out": room.get("check_out") or end,
                    "guests": [
                        g
                        for g in guests
                        if g["room_ref"] == ref
                        or quantity == 1
                        and room.get("hash")
                        and g["room_ref"] == room["hash"]
                    ],
                    "meal": meal_text or itinerary.get("meal_plan", ""),
                    "price": price if isinstance(price, dict) else None,
                    "status": item.get("status") or booking.get("normalized_status") or service.status,
                    "cancellation_terms": room.get("cancellation_rules")
                    or room.get("cancellation")
                    or room.get("penalty_info")
                    or room.get("penalty")
                    or policy,
                }
            )
    if len(rooms) == 1:
        rooms[0]["guests"] = guests
    try:
        nights = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    except (ValueError, TypeError):
        nights = None
    references = [
        str((item.get("providerData") or {}).get("providerReferenceId") or item.get("confirmation_number"))
        for item in items
        if (item.get("providerData") or {}).get("providerReferenceId") or item.get("confirmation_number")
    ]
    from documents.hotel_certificate import hotel_capabilities

    return {
        "documents": hotel_capabilities(snapshot),
        "service_id": str(service.id),
        "order_id": str(service.order_id),
        "version": service.version,
        "hotel": (hb.get("hotel") or {}).get("name") or itinerary.get("property_name") or service.title,
        "address": details.get("address") or itinerary.get("address") or "",
        "supplier": service.supplier.name if service.supplier else "Не указан",
        "supplier_id": str(service.supplier_id) if service.supplier_id else None,
        "locator": booking.get("locator") or service.external_id,
        "supplier_booking_reference": str(
            booking.get("order_id") or booking.get("reference_number") or service.external_id or ""
        ),
        "confirmation_references": references,
        "booking_status": booking.get("normalized_status") or service.status,
        "service_status": service.status,
        "check_in": start,
        "check_out": end,
        "nights": nights,
        "room_count": len(rooms),
        "guest_count": len(guests),
        "rooms": rooms,
        "unassigned_guests": [g for g in guests if not any(g in r["guests"] for r in rooms)],
        "financial": financial_data(service),
        "cancellation_policy": deepcopy(policy),
        "synced_at": booking.get("updated_at") or booking.get("synced_at"),
    }


def permissions(user):
    modify = has_permission(user, "orders.change") and has_service_action(user, "hotel", "book")
    return {
        "view": has_permission(user, "orders.view"),
        "modify": modify,
        "cancel": has_permission(user, "orders.change")
        and has_permission(user, "services.cancel")
        and has_service_action(user, "hotel", "cancel"),
        "financial_confirmation": has_permission(user, "finance.approve_payment"),
        "documents": has_permission(user, "documents.view"),
        "generate_documents": has_permission(user, "documents.view")
        and has_permission(user, "documents.generate"),
    }
