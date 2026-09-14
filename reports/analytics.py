"""Сводная отчётность по услугам: выручка, себестоимость, прибыль.

Приложение называлось `reports`, но отдавало единственный маршрут — сводку для
главной страницы. Здесь собран разрез данных, который нужен руководителю:
по периодам, операторам, поставщикам, видам услуг и клиентам.

Суммы не складываются между валютами: каждая строка отчёта — пара
(группа, валюта). Складывать разные валюты без курса на дату операции нельзя,
а курсы организации заполнены не всегда.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek

#: Статусы, в которых услуга считается проданной.
REALIZED_STATUSES = ("booked", "confirmed", "issued")

GROUPINGS = ("period", "operator", "supplier", "kind", "client")
PERIODS = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def _zero():
    return Value(Decimal("0"), output_field=_MONEY)


@dataclass(frozen=True)
class ReportRow:
    group: str
    group_id: str
    currency: str
    services: int
    revenue: Decimal
    cost: Decimal
    profit: Decimal

    def as_dict(self) -> dict:
        average = self.revenue / self.services if self.services else Decimal("0")
        margin = (self.profit / self.revenue * 100) if self.revenue else Decimal("0")
        return {
            "group": self.group,
            "group_id": self.group_id,
            "currency": self.currency,
            "services": self.services,
            "revenue": str(self.revenue.quantize(Decimal("0.01"))),
            "cost": str(self.cost.quantize(Decimal("0.01"))),
            "profit": str(self.profit.quantize(Decimal("0.01"))),
            "average_check": str(average.quantize(Decimal("0.01"))),
            "margin_percent": str(margin.quantize(Decimal("0.01"))),
        }


def base_queryset(user, params):
    """Услуги, видимые пользователю, с применёнными фильтрами отчёта."""
    from orders.selectors import orders_visible_to
    from services.models import OrderService

    orders = orders_visible_to(user).values("id")
    queryset = OrderService.objects.filter(tenant_id=user.tenant_id, order_id__in=orders)

    statuses = params.getlist("status") if hasattr(params, "getlist") else params.get("status")
    queryset = queryset.filter(status__in=statuses or REALIZED_STATUSES)

    if date_from := params.get("from"):
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to := params.get("to"):
        queryset = queryset.filter(created_at__date__lte=date_to)
    if kind := params.get("kind"):
        queryset = queryset.filter(kind=kind)
    if supplier := params.get("supplier"):
        queryset = queryset.filter(supplier_id=supplier)
    if operator := params.get("operator"):
        queryset = queryset.filter(order__operator_id=operator)
    return queryset


_GROUP_FIELDS = {
    "operator": ("order__operator_id", "order__operator__first_name", "order__operator__last_name", "order__operator__email"),
    "supplier": ("supplier_id", "supplier__name"),
    "kind": ("kind",),
    "client": ("order__client_company_id", "order__client_company__short_name", "order__client_company__legal_name", "order__client_person_id", "order__client_person__given_name", "order__client_person__surname"),
}

_KIND_LABELS = {
    "avia": "Авиа", "rail": "ЖД", "hotel": "Гостиницы", "transfer": "Трансферы",
    "visa": "Визы", "insurance": "Страхование", "tour": "Туры", "other": "Прочее",
}


def _label_for(grouping: str, row: dict) -> tuple[str, str]:
    if grouping == "period":
        bucket = row["bucket"]
        value = bucket.date() if hasattr(bucket, "date") else bucket
        return (value.isoformat() if isinstance(value, date) else str(value)), str(value)
    if grouping == "operator":
        name = " ".join(
            part for part in (row["order__operator__first_name"], row["order__operator__last_name"]) if part
        ).strip()
        return (name or row["order__operator__email"] or "Без оператора"), str(row["order__operator_id"] or "")
    if grouping == "supplier":
        return (row["supplier__name"] or "Без поставщика"), str(row["supplier_id"] or "")
    if grouping == "kind":
        return _KIND_LABELS.get(row["kind"], row["kind"]), row["kind"]
    if grouping == "client":
        company = row["order__client_company__short_name"] or row["order__client_company__legal_name"]
        if company:
            return company, str(row["order__client_company_id"])
        person = " ".join(
            part for part in (row["order__client_person__surname"], row["order__client_person__given_name"]) if part
        ).strip()
        return (person or "Без клиента"), str(row["order__client_person_id"] or "")
    return "", ""


def build_report(user, params) -> dict:
    grouping = params.get("group_by", "period")
    if grouping not in GROUPINGS:
        grouping = "period"
    period = params.get("period", "month")
    if period not in PERIODS:
        period = "month"

    queryset = base_queryset(user, params)

    if grouping == "period":
        queryset = queryset.annotate(bucket=PERIODS[period]("created_at"))
        group_fields = ("bucket",)
    else:
        group_fields = _GROUP_FIELDS[grouping]

    aggregated = (
        queryset.values(*group_fields, "currency")
        .annotate(
            services=Count("id"),
            revenue=Coalesce(Sum("client_total"), _zero()),
            cost=Coalesce(Sum("supplier_cost"), _zero()),
        )
        .annotate(profit=F("revenue") - F("cost"))
        .order_by(*group_fields, "currency")
    )

    rows = []
    for row in aggregated:
        label, group_id = _label_for(grouping, row)
        rows.append(
            ReportRow(
                group=label,
                group_id=group_id,
                currency=row["currency"],
                services=row["services"],
                revenue=row["revenue"] or Decimal("0"),
                cost=row["cost"] or Decimal("0"),
                profit=row["profit"] or Decimal("0"),
            )
        )

    totals: dict[str, dict] = {}
    for row in rows:
        bucket = totals.setdefault(
            row.currency,
            {"currency": row.currency, "services": 0, "revenue": Decimal("0"), "cost": Decimal("0")},
        )
        bucket["services"] += row.services
        bucket["revenue"] += row.revenue
        bucket["cost"] += row.cost

    return {
        "group_by": grouping,
        "period": period if grouping == "period" else None,
        "filters": {
            "from": params.get("from", ""),
            "to": params.get("to", ""),
            "kind": params.get("kind", ""),
            "supplier": params.get("supplier", ""),
            "operator": params.get("operator", ""),
        },
        "rows": [row.as_dict() for row in rows],
        "totals": [
            {
                "currency": bucket["currency"],
                "services": bucket["services"],
                "revenue": str(bucket["revenue"].quantize(Decimal("0.01"))),
                "cost": str(bucket["cost"].quantize(Decimal("0.01"))),
                "profit": str((bucket["revenue"] - bucket["cost"]).quantize(Decimal("0.01"))),
            }
            for bucket in totals.values()
        ],
    }
