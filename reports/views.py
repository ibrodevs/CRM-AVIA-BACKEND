"""Dashboard: агрегаты под роль и пользователя (ТЗ §19)."""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.money import money_dict


class DashboardView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request):
        from integrations.models import IntegrationIncident
        from orders.models import OrderTask
        from orders.views import orders_visible_to
        from workforce.models import SlaInstance

        now = timezone.now()
        scope = request.query_params.get("role_scope", "my")
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")

        orders = orders_visible_to(request.user)
        if scope == "my":
            orders = orders.filter(operator=request.user)
        if date_from:
            orders = orders.filter(created_at__date__gte=date_from)
        if date_to:
            orders = orders.filter(created_at__date__lte=date_to)

        status_breakdown = dict(
            orders.values_list("status").annotate(count=Count("id"))
        )

        # сегодняшние поездки
        from calendar_app.models import Trip

        today = now.date()
        trips_today = Trip.objects.filter(
            tenant_id=request.user.tenant_id,
            starts_at__date=today, archived_at__isnull=True,
        ).select_related("order")

        # мои задачи
        my_tasks = OrderTask.objects.filter(
            tenant_id=request.user.tenant_id, assignee=request.user,
            status__in=["open", "in_progress"],
        ).order_by("due_at")[:20]

        # SLA queue
        sla = SlaInstance.objects.filter(
            tenant_id=request.user.tenant_id, resolved_at__isnull=True,
        )
        if scope == "my":
            sla = sla.filter(assignee=request.user)
        sla_breached = sla.filter(Q(breached_at__isnull=False)
                                  | Q(response_deadline__lt=now,
                                      responded_at__isnull=True)).count()

        # финансовая сводка
        finance_summary = {}
        from accounts.permissions import has_permission

        if has_permission(request.user, "finance.view"):
            from finance.models import FinancialObligation

            receivable = FinancialObligation.objects.filter(
                tenant_id=request.user.tenant_id, direction="client_receivable",
                status__in=["open", "partial"],
            ).values("currency").annotate(total=Sum("original_amount") - Sum("paid_amount"))
            overdue = FinancialObligation.objects.filter(
                tenant_id=request.user.tenant_id, direction="client_receivable",
                status__in=["open", "partial"], due_date__lt=today,
            ).count()
            finance_summary = {
                "receivable": [money_dict(r["total"], r["currency"]) for r in receivable],
                "overdue_obligations": overdue,
            }

        # ошибки поставщиков/интеграций
        incidents = IntegrationIncident.objects.filter(
            tenant_id=request.user.tenant_id,
            status__in=["open", "assigned", "reopened", "escalated"],
        )

        # attention feed: дедлайны в ближайшие 24 часа
        from services.models import OrderService

        upcoming_deadlines = OrderService.objects.filter(
            tenant_id=request.user.tenant_id,
            ticketing_deadline__gt=now,
            ticketing_deadline__lte=now + timedelta(hours=24),
            status__in=["booked", "confirmed"],
        ).select_related("order")

        return Response({
            "calculated_at": now,
            "orders": {
                "total": orders.count(),
                "by_status": status_breakdown,
                "new_today": orders.filter(created_at__date=today).count(),
            },
            "trips_today": [
                {"id": str(t.id), "order_number": t.order.number, "title": t.title,
                 "starts_at": t.starts_at, "criticality": t.criticality}
                for t in trips_today[:20]
            ],
            "my_tasks": [
                {"id": str(t.id), "title": t.title, "due_at": t.due_at,
                 "priority": t.priority, "order": str(t.order_id)}
                for t in my_tasks
            ],
            "sla": {"open": sla.count(), "breached": sla_breached},
            "finance": finance_summary,
            "integration_incidents": {
                "open": incidents.count(),
                "critical": incidents.filter(severity="critical").count(),
            },
            "attention": [
                {"type": "ticketing_deadline", "service": str(s.id),
                 "order_number": s.order.number, "deadline": s.ticketing_deadline}
                for s in upcoming_deadlines[:20]
            ],
        })
