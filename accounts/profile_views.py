from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from aftersales.models import AfterSaleCase
from common.errors import ApiError
from orders.models import Order
from services.models import OrderService
from workforce.models import MotivationAccrual, SlaInstance


class MyStatisticsView(APIView):
    def get(self, request):
        try:
            zone = ZoneInfo(request.user.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("Asia/Bishkek")
        today = timezone.localdate(timezone.now(), zone)
        try:
            start = date.fromisoformat(request.query_params.get("from", today.replace(day=1).isoformat()))
            end = date.fromisoformat(request.query_params.get("to", today.isoformat()))
        except ValueError:
            raise ApiError(code="VALIDATION_ERROR", message="Некорректный период", status_code=400) from None
        if start > end:
            raise ApiError(code="VALIDATION_ERROR", message="Начало периода позже окончания", status_code=400)
        lower = datetime.combine(start, time.min, zone)
        if end == date.max:
            raise ApiError(code="VALIDATION_ERROR", message="Период слишком большой", status_code=400)
        upper = datetime.combine(end + timedelta(days=1), time.min, zone)
        scope = {"tenant_id": request.user.tenant_id, "archived_at__isnull": True, "created_at__gte": lower, "created_at__lt": upper}
        orders = Order.objects.filter(**scope, operator=request.user)
        services = OrderService.objects.filter(**scope).filter(Q(responsible=request.user) | Q(responsible__isnull=True, order__operator=request.user))
        cases = AfterSaleCase.objects.filter(**scope, responsible=request.user)
        profit = defaultdict(Decimal)
        for service in services.filter(status="issued"):
            profit[service.currency] += (service.client_total or Decimal(0)) - (service.supplier_cost or Decimal(0))
        earnings = defaultdict(Decimal)
        for accrual in MotivationAccrual.objects.filter(**scope, user=request.user, reversed_at__isnull=True):
            earnings[accrual.currency] += accrual.amount
        response_times = [max(0, (item.responded_at - item.started_at).total_seconds() / 60) for item in SlaInstance.objects.filter(tenant_id=request.user.tenant_id, assignee=request.user, started_at__gte=lower, started_at__lt=upper, responded_at__isnull=False)]
        return Response({"from": start, "to": end, "orders": orders.count(), "issued": services.filter(status="issued").count(), "exchanges": cases.filter(type="exchange").count(), "refunds": cases.filter(type="refund").count(), "response_minutes": round(sum(response_times) / len(response_times)) if response_times else None, "profit": {k: str(v) for k, v in profit.items()}, "earnings": {k: str(v) for k, v in earnings.items()}})
