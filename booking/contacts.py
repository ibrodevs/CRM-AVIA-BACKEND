from integrations.adapters import AdapterError, adapter_for_service
from orders.contacts import resolve_order_contact


def requires_booking_contact(services):
    for service in services:
        try:
            if getattr(adapter_for_service(service), "requires_booking_contact", False):
                return True
        except AdapterError:
            # Provider configuration failures are handled by the normal preflight path.
            continue
    return False


def booking_contact_error(order, services):
    if not requires_booking_contact(services):
        return None
    contact = resolve_order_contact(order)
    if not contact["valid"]:
        return {"code": "CONTACT_REQUIRED", "message": contact["message"]}
    fields = set()
    for service in services:
        try:
            fields.update(getattr(adapter_for_service(service), "required_booking_contact_fields", ()))
        except AdapterError:
            pass
    labels = {"email": "email", "phone": "телефон"}
    missing = [
        labels.get(field, field) for field in sorted(fields) if not str(contact.get(field) or "").strip()
    ]
    if missing:
        return {
            "code": "CONTACT_DATA_MISSING",
            "message": "Для выбранного поставщика заполните " + ", ".join(missing) + " контактного лица.",
        }
    return None
