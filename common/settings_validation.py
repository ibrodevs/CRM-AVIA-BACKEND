from decimal import Decimal

from rest_framework import serializers


class CurrencyInput(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Z]{3}$")
    name = serializers.CharField(max_length=100)
    sym = serializers.CharField(max_length=12, allow_blank=True, required=False)
    rate = serializers.JSONField(required=False)


def validate_currency_settings(value):
    base = serializers.RegexField(r"^[A-Z]{3}$").run_validation(value.get("base"))
    currencies = value.get("currencies")
    if currencies is not None:
        serializer = CurrencyInput(data=currencies, many=True)
        serializer.is_valid(raise_exception=True)
        codes = [row["code"] for row in serializer.validated_data]
        if len(codes) != len(set(codes)) or base not in codes:
            raise serializers.ValidationError("Основная валюта должна присутствовать, коды не должны повторяться")
    rates = value.get("rates", {})
    if not isinstance(rates, dict):
        raise serializers.ValidationError("Курсы должны быть объектом")
    rate_field = serializers.DecimalField(max_digits=18, decimal_places=8, min_value=Decimal("0.00000001"))
    for code, rate in rates.items():
        serializers.RegexField(r"^[A-Z]{3}$").run_validation(code)
        rate_field.run_validation(rate)
    return {**value, "base": base, "rates": {**rates, base: 1}}
