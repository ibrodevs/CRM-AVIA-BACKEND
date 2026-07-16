"""Деньги: только Decimal, ISO 4217, документированное округление.

Округление всех денежных расчётов — ROUND_HALF_UP до minor units валюты
(ТЗ §6.3). float в денежных путях запрещён.
"""
from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

# Валюты с нестандартным числом знаков после запятой (по умолчанию 2).
_MINOR_UNITS: dict[str, int] = {
    "JPY": 0, "KRW": 0, "VND": 0, "ISK": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3,
}


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get(currency.upper(), 2)


def quantize(amount: Decimal, currency: str) -> Decimal:
    """Округляет сумму до minor units валюты по ROUND_HALF_UP."""
    if isinstance(amount, float):
        raise TypeError("float запрещён в денежных расчётах — используйте Decimal")
    exp = Decimal(1).scaleb(-minor_units(currency))
    return Decimal(amount).quantize(exp, rounding=ROUND_HALF_UP)


class MoneyField(serializers.Field):
    """Сериализует пару (amount, currency) в {"amount": "1720.00", "currency": "USD"}.

    Использование: MoneyField(amount_field="client_total", currency_field="currency").
    """

    def __init__(self, amount_field: str, currency_field: str = "currency", **kwargs):
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)
        self.amount_field = amount_field
        self.currency_field = currency_field

    def get_attribute(self, instance):
        return instance

    def to_representation(self, instance):
        amount = getattr(instance, self.amount_field, None)
        currency = getattr(instance, self.currency_field, None)
        if amount is None or not currency:
            return None
        return {"amount": str(quantize(amount, currency)), "currency": currency}


def money_dict(amount: Decimal | None, currency: str | None) -> dict | None:
    if amount is None or not currency:
        return None
    return {"amount": str(quantize(amount, currency)), "currency": currency}
