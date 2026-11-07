"""HBPRO static remarks and cancellation policy amounts; no network mutations."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.adapters import AdapterError


def remark_catalog(adapter, ctx):
    _, config = adapter._config(ctx)
    rows = adapter._dictionary(ctx, config, "hotel/remarks")
    return [
        {k: row[k] for k in ("id", "title", "dataType")}
        for row in rows
        if row.get("trash") is False
        and type(row.get("id")) is int
        and row.get("dataType") in ("NONE", "STRING", "DATETIME")
        and isinstance(row.get("title"), str)
    ]


def validated_remarks(adapter, ctx, value):
    if not isinstance(value, list) or len(value) > 30:
        raise AdapterError("INVALID_REMARK", "Выберите пожелания из справочника HBPRO", category="validation")
    if not value:
        return []
    catalog = {row["id"]: row for row in remark_catalog(adapter, ctx)}
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"remarkId", "data"}:
            raise AdapterError("INVALID_REMARK", "Некорректное пожелание HBPRO", category="validation")
        key = item.get("remarkId")
        if type(key) is not int or key not in catalog or key in seen:
            raise AdapterError("INVALID_REMARK", "Пожелание недоступно или выбрано повторно", category="validation")
        seen.add(key)
        data = item.get("data")
        kind = catalog[key]["dataType"]
        if kind == "NONE":
            if data not in (None, ""):
                raise AdapterError("INVALID_REMARK", "Это пожелание не принимает дополнительный текст", category="validation")
            data = None
        elif not isinstance(data, str) or not data.strip() or len(data) > 2000 or any(ord(c) < 32 for c in data):
            raise AdapterError("INVALID_REMARK", "Заполните значение выбранного пожелания", category="validation")
        elif kind == "DATETIME":
            try:
                valid = parse_datetime(data) is not None
            except ValueError:
                valid = False
            if not valid:
                raise AdapterError("INVALID_REMARK", "Укажите дату и время пожелания", category="validation")
        result.append({"remarkId": key, "data": data})
    return result


def cancellation_quote(items, currency, now=None):
    """Evaluate dated supplier policies, never invent zero for absent/invalid data."""
    now = now or timezone.now()
    expiry = now + timedelta(minutes=5)
    total, details, policies = Decimal(0), [], []
    exact = bool(items)
    for item in items:
        rows = (item.get("finePolicies") or {}).get("cancel")
        policies.append({"itemId": item.get("itemId"), "cancel": rows})
        amount, schedule = None, []
        if isinstance(rows, list) and rows:
            try:
                for row in rows:
                    # Some schema revisions describe strings. Only structured,
                    # unambiguous amounts/dates can authorize an API cancellation.
                    if not isinstance(row, dict):
                        raise ValueError
                    if "from" not in row:
                        raise ValueError
                    money = row.get("price") or row.get("amount") or {}
                    value = Decimal(str(money["amount"]))
                    if not value.is_finite() or value < 0 or money["currency"] != currency:
                        raise ValueError
                    start = parse_datetime(row["from"]) if row.get("from") else None
                    if row.get("from") and (start is None or timezone.is_naive(start)):
                        raise ValueError
                    schedule.append((start, value))
                active = [(start, value) for start, value in schedule if start is None or start <= now]
                amount = max(active, key=lambda pair: pair[0] or timezone.datetime.min.replace(tzinfo=now.tzinfo))[1] if active else Decimal(0)
                future = [start for start, _ in schedule if start is not None and start > now]
                if future:
                    expiry = min(expiry, min(future))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                amount = None
        exact = exact and amount is not None and bool(item.get("itemId"))
        if amount is not None:
            total += amount
        display_rows = [
            {**{k: v for k, v in row.items() if k != "amount"}, "price": row.get("price") or row.get("amount") or {}}
            if isinstance(row, dict) else row
            for row in rows
        ] if isinstance(rows, list) else rows
        details.append({"item_id": item.get("itemId"), "penalty": str(amount) if amount is not None else None, "policy": display_rows})
    return {
        "penalty": str(total) if exact else None,
        "currency": currency,
        "penalty_exact": exact,
        "quoted_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "source": "booking_items",
        "items": details,
        "policy_fingerprint": hashlib.sha256(json.dumps(policies, sort_keys=True).encode()).hexdigest(),
    }


def check_quote(expected, actual):
    expiry = parse_datetime(expected.get("expires_at", ""))
    if not expiry or timezone.is_naive(expiry) or expiry <= timezone.now():
        raise AdapterError("CANCELLATION_QUOTE_EXPIRED", "Обновите штраф перед отменой", category="validation")
    fields = ("penalty", "currency", "penalty_exact", "policy_fingerprint")
    if not actual.get("penalty_exact") or any(expected.get(k) != actual.get(k) for k in fields):
        raise AdapterError("CANCELLATION_QUOTE_CHANGED", "Условия отмены изменились. Подтвердите актуальный штраф заново", category="validation")
