from django.db import models

from common.models import TenantModel


class TravelPolicy(TenantModel):
    company = models.ForeignKey("crm.Company", on_delete=models.CASCADE, related_name="travel_policies")
    name = models.CharField(max_length=150)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    policy_version = models.PositiveIntegerField(default=1)
    scopes = models.JSONField(default=list, blank=True)

    allowed_avia_cabins = models.JSONField(default=list, blank=True)
    allowed_airlines = models.JSONField(default=list, blank=True)
    allowed_rail_classes = models.JSONField(default=list, blank=True)
    allowed_train_types = models.JSONField(default=list, blank=True)
    allowed_hotel_categories = models.JSONField(default=list, blank=True)
    allowed_hotel_chains = models.JSONField(default=list, blank=True)
    allowed_meal_plans = models.JSONField(default=list, blank=True)
    allowed_car_classes = models.JSONField(default=list, blank=True)
    price_limits = models.JSONField(default=dict, blank=True)
    min_advance_booking_days = models.PositiveSmallIntegerField(null=True, blank=True)
    approver_chain = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "travel_policy_policy"


class PolicyCheckResult:
    """Результат проверки оффера: allowed/warning/approval_required/forbidden."""

    ALLOWED = "allowed"
    WARNING = "warning"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"

    def __init__(self):
        self.violations: list[dict] = []
        self.verdict = self.ALLOWED

    def add(self, rule: str, message: str, severity: str) -> None:
        self.violations.append({"rule": rule, "message": message, "severity": severity})
        order = [self.ALLOWED, self.WARNING, self.APPROVAL_REQUIRED, self.FORBIDDEN]
        if order.index(severity) > order.index(self.verdict):
            self.verdict = severity

    def as_dict(self, policy: TravelPolicy | None = None) -> dict:
        return {
            "verdict": self.verdict,
            "violations": self.violations,
            "approver_chain": policy.approver_chain
            if policy is not None and self.verdict == self.APPROVAL_REQUIRED
            else [],
        }


def check_offer_compliance(policy: TravelPolicy, offer: dict) -> PolicyCheckResult:
    """Проверяет нормализованный оффер против политики.

    offer — словарь с полями kind, cabin, airline, rail_class, hotel_category,
    meal_plan, price {amount, currency}, advance_days и т.п.
    Запрещённый вариант нельзя забронировать без override с причиной.
    """
    from decimal import Decimal

    result = PolicyCheckResult()
    kind = offer.get("kind", "")

    def _listed(value, allowed, rule, label):
        if allowed and value and value not in allowed:
            result.add(rule, f"{label} «{value}» вне политики", PolicyCheckResult.APPROVAL_REQUIRED)

    if kind == "avia":
        _listed(offer.get("cabin"), policy.allowed_avia_cabins, "avia_cabin", "Класс обслуживания")
        _listed(offer.get("airline"), policy.allowed_airlines, "airline", "Авиакомпания")
    elif kind == "rail":
        _listed(offer.get("rail_class"), policy.allowed_rail_classes, "rail_class", "Класс вагона")
        _listed(offer.get("train_type"), policy.allowed_train_types, "train_type", "Тип поезда")
    elif kind == "hotel":
        _listed(
            str(offer.get("hotel_category", "")),
            [str(c) for c in policy.allowed_hotel_categories],
            "hotel_category",
            "Категория отеля",
        )
        _listed(offer.get("meal_plan"), policy.allowed_meal_plans, "meal_plan", "Питание")

    limit = (policy.price_limits or {}).get(kind)
    price = offer.get("price") or {}
    if limit and price.get("amount") is not None:
        if str(price.get("currency")) != str(limit.get("currency")):
            result.add(
                "price_limit_currency",
                "Валюта предложения отличается от валюты лимита; требуется проверка",
                PolicyCheckResult.WARNING,
            )
        elif Decimal(str(price["amount"])) > Decimal(str(limit["amount"])):
            result.add(
                "price_limit",
                f"Стоимость превышает лимит {limit['amount']} {limit['currency']}",
                PolicyCheckResult.APPROVAL_REQUIRED,
            )

    advance = offer.get("advance_days")
    if policy.min_advance_booking_days and advance is not None:
        if int(advance) < policy.min_advance_booking_days:
            result.add(
                "advance_booking",
                f"Бронирование менее чем за {policy.min_advance_booking_days} дней",
                PolicyCheckResult.WARNING,
            )

    return result


class PolicyOverride(TenantModel):
    """Разрешённый override запрещённого варианта с причиной (ТЗ §6.3)."""

    policy = models.ForeignKey(TravelPolicy, on_delete=models.PROTECT, related_name="overrides")
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.CASCADE, related_name="policy_overrides"
    )
    service = models.ForeignKey(
        "services.OrderService",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="policy_overrides",
    )
    reason = models.TextField()
    approved_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    violations = models.JSONField(default=list)

    class Meta:
        db_table = "travel_policy_override"
