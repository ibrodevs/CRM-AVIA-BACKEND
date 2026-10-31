"""Bind Hotelbook preflight approval to the actual inputs dispatched by the worker."""

import hashlib
import json


def fingerprint(workflow, service):
    from booking.job_handlers import _booking_contact, _booking_customer, _booking_passengers

    workflow.order.refresh_from_db()
    passengers = sorted(_booking_passengers(service), key=lambda value: json.dumps(value, sort_keys=True))
    data = {
        "supplier": str(service.supplier_id),
        "version": service.version,
        "snapshot": service.provider_snapshot,
        "passengers": passengers,
        "contact": _booking_contact(workflow.order),
        "customer": _booking_customer(workflow.order),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def preflight_error(workflow, service):
    snapshot = service.provider_snapshot or {}
    if snapshot.get("provider_adapter") != "hotelbook" or snapshot.get("hotelbook_booking"):
        return None
    accepted = (workflow.preflight_result or {}).get("hotelbook_inputs", {}).get(str(service.pk))
    if not accepted or accepted != fingerprint(workflow, service):
        return (
            "Данные бронирования Hotelbook изменились; выполните preflight и подтвердите актуальные условия"
        )
    return None
