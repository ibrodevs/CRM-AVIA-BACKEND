"""API определения сервисного сбора по финансовым условиям контрагента."""

from __future__ import annotations

from datetime import datetime

from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.errors import ApiError
from crm.fee_resolution import resolve_service_fee, to_decimal
from crm.models import Company

MAX_ITEMS = 200


def _company_for_request(user, data: dict) -> tuple[Company | None, str]:
    """Контрагент запроса: явная компания либо юрлицо-заказчик заказа."""
    company_id = data.get("company")
    if company_id:
        company = Company.objects.filter(
            tenant_id=user.tenant_id, pk=company_id, archived_at__isnull=True
        ).first()
        if company is None:
            raise ApiError(code="NOT_FOUND", message="Юрлицо не найдено", status_code=404)
        return company, "company"

    order_id = data.get("order")
    if order_id:
        from orders.models import Order

        order = Order.objects.filter(tenant_id=user.tenant_id, pk=order_id).first()
        if order is None:
            raise ApiError(code="NOT_FOUND", message="Заказ не найден", status_code=404)
        if order.client_company_id:
            company = Company.objects.filter(
                tenant_id=user.tenant_id, pk=order.client_company_id, archived_at__isnull=True
            ).first()
            if company is not None:
                return company, "order"
        # Заказ физлица — договорных условий нет, сбор назначает оператор.
        return None, "order_person" if order.client_person_id else "order"
    if data.get("person"):
        return None, "person"
    return None, "none"


def _resolution_date(value):
    if value in (None, ""):
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ApiError(
        code="VALIDATION_ERROR",
        message="Некорректная дата расчёта",
        fields={"date": ["Используйте формат ДД.ММ.ГГГГ"]},
        status_code=400,
    )


class ServiceFeeResolveView(APIView):
    """Сервисный сбор бланка: договорной либо ручной, с явной причиной."""

    permission_classes = [require("crm.view")]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        company, context = _company_for_request(request.user, data)
        on_date = _resolution_date(data.get("date"))
        items = data.get("items")

        if items is None:
            resolution = resolve_service_fee(
                company=company,
                service_kind=data.get("service_kind"),
                base_amount=to_decimal(data.get("base_amount")),
                currency=str(data.get("currency") or ""),
                on_date=on_date,
            )
            return Response({"context": context, **resolution})

        if not isinstance(items, list):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="items должен быть массивом бланков",
                fields={"items": ["Ожидается массив"]},
                status_code=400,
            )
        if len(items) > MAX_ITEMS:
            raise ApiError(
                code="VALIDATION_ERROR",
                message=f"За один запрос можно рассчитать не более {MAX_ITEMS} бланков",
                fields={"items": [f"Не более {MAX_ITEMS} элементов"]},
                status_code=400,
            )

        results = []
        for index, item in enumerate(items):
            row = item if isinstance(item, dict) else {}
            resolution = resolve_service_fee(
                company=company,
                service_kind=row.get("service_kind", data.get("service_kind")),
                base_amount=to_decimal(row.get("base_amount")),
                currency=str(row.get("currency") or data.get("currency") or ""),
                on_date=on_date,
            )
            results.append({"key": row.get("key", index), **resolution})
        return Response({"context": context, "results": results})
