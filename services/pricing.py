from decimal import Decimal

from django.utils import timezone

from common.money import quantize
from services.models import PriceSnapshot


def resolve_markup_rules(
    supplier,
    *,
    kind: str,
    route: str = "",
    airline: str = "",
    cabin: str = "",
    passenger_category: str = "",
    on_date=None,
):
    """Возвращает применимые правила наценки поставщика по приоритету с объяснением."""
    if supplier is None:
        return []
    on_date = on_date or timezone.now().date()
    rules = supplier.markup_rules.filter(archived_at__isnull=True).order_by("priority")
    applicable = []
    for rule in rules:
        if rule.service_kind not in ("*", kind):
            continue
        if rule.effective_from and rule.effective_from > on_date:
            continue
        if rule.effective_to and rule.effective_to < on_date:
            continue
        if rule.airline and rule.airline != airline:
            continue
        if rule.cabin and rule.cabin != cabin:
            continue
        if rule.passenger_category and rule.passenger_category != passenger_category:
            continue
        if rule.route and not _route_matches(rule.route, route):
            continue
        applicable.append(rule)
    return applicable[:1]


def _route_matches(pattern: str, route: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(route.upper(), pattern.upper())


def calculate_price(
    *,
    base: Decimal,
    taxes: Decimal = Decimal(0),
    currency: str,
    fee_rules=None,
    markup_rules=None,
    discount: Decimal = Decimal(0),
    rate_source: str = "",
    tenant_id=None,
    service=None,
    offer=None,
    step: str = "search",
    user=None,
) -> dict:
    """Рассчитывает итог и сохраняет PriceSnapshot. Все Decimal, ROUND_HALF_UP."""
    components: list[dict] = [
        {"name": "base", "amount": str(quantize(base, currency))},
        {"name": "taxes", "amount": str(quantize(taxes, currency))},
    ]
    formula_parts = ["base", "taxes"]
    total = base + taxes

    for rule in fee_rules or []:
        if rule.calculation == "percent":
            amount = quantize(base * rule.value / Decimal(100), currency)
        else:
            amount = quantize(rule.value, currency)
        total += amount
        components.append(
            {
                "name": f"fee:{rule.fee_kind}",
                "amount": str(amount),
                "rule_id": str(rule.id),
                "calculation": rule.calculation,
                "value": str(rule.value),
            }
        )
        formula_parts.append(f"fee_{rule.fee_kind}({rule.calculation}:{rule.value})")

    for rule in markup_rules or []:
        if rule.amount_type == "percent":
            amount = quantize(base * rule.amount_value / Decimal(100), currency)
        else:
            amount = quantize(rule.amount_value, currency)
        total += amount
        components.append(
            {
                "name": "supplier_markup",
                "amount": str(amount),
                "rule_id": str(rule.id),
                "priority": rule.priority,
                "explanation": f"{rule.amount_type}:{rule.amount_value} "
                f"({rule.service_kind}{'/' + rule.route if rule.route else ''})",
            }
        )
        formula_parts.append(f"markup({rule.amount_type}:{rule.amount_value})")

    if discount:
        discount_q = quantize(discount, currency)
        total -= discount_q
        components.append({"name": "discount", "amount": str(-discount_q)})
        formula_parts.append("-discount")

    total = quantize(max(total, Decimal(0)), currency)
    snapshot = None
    if tenant_id is not None:
        snapshot = PriceSnapshot.objects.create(
            tenant_id=tenant_id,
            service=service,
            offer=offer,
            step=step,
            components={"items": components},
            formula=" + ".join(formula_parts),
            rate_source=rate_source,
            rate_timestamp=timezone.now(),
            currency=currency,
            total=total,
            created_by=user,
        )
    return {
        "total": total,
        "currency": currency,
        "components": components,
        "snapshot_id": snapshot.id if snapshot else None,
    }
