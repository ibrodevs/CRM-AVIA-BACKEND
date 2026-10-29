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
    return None if contact["valid"] else {"code": "CONTACT_REQUIRED", "message": contact["message"]}
