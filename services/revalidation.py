"""Persist an adapter's normalized offer using the existing pricing snapshots."""

from decimal import Decimal

from services.pricing import calculate_price, resolve_markup_rules


def _pricing(snapshot, supplier, tenant_id, *, offer=None, service=None):
    price = snapshot["price"]
    rules = resolve_markup_rules(supplier, kind=snapshot.get("kind", service.kind if service else offer.kind))
    return calculate_price(
        base=Decimal(price["amount"]),
        currency=price["currency"],
        markup_rules=rules,
        tenant_id=tenant_id,
        offer=offer,
        service=service,
        step="revalidate",
    )


def update_offer(offer, result):
    snapshot = result.get("snapshot")
    if not snapshot:
        return
    pricing = _pricing(snapshot, offer.supplier, offer.tenant_id, offer=offer)
    offer.raw_snapshot = snapshot
    offer.itinerary = snapshot["itinerary"]
    offer.fare = snapshot.get("fare")
    offer.availability = snapshot["availability"]
    offer.price_amount = pricing["total"]
    offer.price_currency = snapshot["price"]["currency"]
    offer.applied_markup_rules = [c for c in pricing["components"] if c["name"] == "supplier_markup"]
    offer.save(
        update_fields=[
            "raw_snapshot",
            "itinerary",
            "fare",
            "availability",
            "price_amount",
            "price_currency",
            "applied_markup_rules",
            "updated_at",
        ]
    )


def update_service(service, result):
    snapshot = result.get("snapshot")
    if not snapshot:
        return None
    if (service.provider_snapshot or {}).get("hotelbook_booking"):
        snapshot = {**snapshot, "hotelbook_booking": service.provider_snapshot["hotelbook_booking"]}
    pricing = _pricing(snapshot, service.supplier, service.tenant_id, service=service)
    new_total = (
        pricing["total"]
        + sum(
            (getattr(service, name) or Decimal(0) for name in ("taxes", "agency_fee", "markup")), Decimal(0)
        )
        - (service.discount or Decimal(0))
    )
    old_total, old_currency = service.client_total, service.currency
    service.provider_snapshot = snapshot
    service.supplier_cost = Decimal(snapshot["price"]["amount"])
    service.client_total = new_total
    service.currency = snapshot["price"]["currency"]
    service.cancellation_rules = result.get("fare_rules")
    service.version += 1
    service.save(
        update_fields=[
            "provider_snapshot",
            "supplier_cost",
            "client_total",
            "currency",
            "cancellation_rules",
            "version",
            "updated_at",
        ]
    )
    if old_total != new_total or old_currency != service.currency:
        return {
            "service_id": str(service.id),
            "old": str(old_total),
            "new": str(new_total),
            "currency": service.currency,
            "old_currency": old_currency,
        }
    return None
