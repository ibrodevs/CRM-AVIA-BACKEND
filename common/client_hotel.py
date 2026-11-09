"""Presentation boundary for hotel material shared with guests.

Supplier snapshots and operator API responses must retain their original data.
"""

import re

_PRIVATE_KEYS = {
    "supplier",
    "suppliername",
    "suppliercost",
    "supplierprice",
    "suppliertotal",
    "supplierreference",
    "supplierbookingreference",
    "supplierorderno",
    "supplierorderid",
    "provider",
    "provideradapter",
    "providername",
    "providersnapshot",
    "rawsnapshot",
    "order",
    "orderid",
    "orderno",
    "ordernumber",
    "crmorderno",
    "crmorderid",
    "serviceid",
    "itemid",
    "itemids",
    "clientorderid",
    "clientrequestid",
    "cost",
    "profit",
    "markup",
    "supplieramount",
    "suppliernetprice",
    "providercost",
    "providerprice",
    "purchaseprice",
    "netprice",
    "netamount",
    "commission",
    "margin",
    "source",
    "token",
    "password",
    "credentials",
}
_PRIVATE_LABEL = re.compile(
    r"^(?:заказ|поставщик|название поставщика)$|(?:стоимость|цена|заказ|бронь|номер).*поставщик|закупоч|себестоимост|маржа|комисси|заказ.*(?:crm|срм|внутрен|api)|"
    r"(?:внутрен|crm|срм).*номер|идентификатор",
    re.I,
)
_VENDOR = re.compile(r"(?<!\w)(?:hb[\s-]?pro|hotel[\s-]?book|хотелбук)(?!\w)", re.I)
_ORDER = re.compile(r"\bORD-[A-Z0-9-]+\b", re.I)


def client_hotel_text(value, private_ids=()):
    text = str(value)
    for identifier in sorted({str(v) for v in private_ids if v}, key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(identifier) + r"(?!\w)", "", text)
    return _ORDER.sub("", _VENDOR.sub("поставщик услуг", text)).strip()


def client_hotel_content(value, private_ids=()):
    """Remove internal fields/labelled rows; preserve guest prices and penalty amounts."""
    if isinstance(value, dict):
        label = value.get("l") or value.get("label") or ""
        if _PRIVATE_LABEL.search(str(label)):
            return None
        return {
            key: cleaned
            for key, child in value.items()
            if re.sub(r"[^a-z0-9]", "", key.lower()) not in _PRIVATE_KEYS
            and (cleaned := client_hotel_content(child, private_ids)) is not None
        }
    if isinstance(value, list):
        return [
            cleaned for child in value if (cleaned := client_hotel_content(child, private_ids)) is not None
        ]
    return client_hotel_text(value, private_ids) if isinstance(value, str) else value


def public_hotel_card(card):
    private_ids = [card.order.number, card.order_id] if card.order_id else []
    if card.service_id:
        booking = (card.service.provider_snapshot or {}).get("hotelbook_booking") or {}
        private_ids += [card.service_id, booking.get("order_id"), booking.get("client_request_id")]
        private_ids += [item.get("itemId") for item in booking.get("items", [])]
    content = client_hotel_content(card.content, private_ids)
    # Legacy drafts carried the entire operator calculation inside `fin`.
    if isinstance(content, dict) and isinstance(content.get("fin"), dict):
        content["fin"] = {
            key: val
            for key, val in content["fin"].items()
            if key in {"clientTotal", "fee", "currency", "model"}
        }
    price = {key: val for key, val in card.price_snapshot.items() if key in {"amount", "currency"}}
    return content, price
