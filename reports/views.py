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
        from orders.selectors import orders_visible_to
        from workforce.models import SlaInstance

        now = timezone.now()
        scope = request.query_params.get("role_scope", "my")
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")

        orders = orders_visible_to(request.user).select_related(
            "client_person", "client_company", "operator"
        )
        if scope == "my":
            orders = orders.filter(operator=request.user)
        if date_from:
            orders = orders.filter(created_at__date__gte=date_from)
        if date_to:
            orders = orders.filter(created_at__date__lte=date_to)

        status_breakdown = dict(orders.values_list("status").annotate(count=Count("id")))
        active_statuses = {
            "new",
            "in_progress",
            "awaiting_confirmation",
            "awaiting_payment",
            "needs_review",
            "on_hold",
            "data_missing",
        }

        from calendar_app.models import Trip

        today = now.date()
        trips_today = Trip.objects.filter(
            tenant_id=request.user.tenant_id,
            starts_at__date=today,
            archived_at__isnull=True,
        ).select_related("order")

        my_tasks = OrderTask.objects.filter(
            tenant_id=request.user.tenant_id,
            assignee=request.user,
            status__in=["open", "in_progress"],
        ).select_related("order").order_by("due_at")[:20]

        sla = SlaInstance.objects.filter(
            tenant_id=request.user.tenant_id,
            resolved_at__isnull=True,
        )
        if scope == "my":
            sla = sla.filter(assignee=request.user)
        sla_breached = sla.filter(
            Q(breached_at__isnull=False) | Q(response_deadline__lt=now, responded_at__isnull=True)
        ).count()

        finance_summary = {}
        from accounts.permissions import has_permission

        if has_permission(request.user, "finance.view"):
            from finance.models import FinancialObligation

            receivable = (
                FinancialObligation.objects.filter(
                    tenant_id=request.user.tenant_id,
                    direction="client_receivable",
                    status__in=["open", "partial"],
                )
                .values("currency")
                .annotate(total=Sum("original_amount") - Sum("paid_amount"))
            )
            overdue = FinancialObligation.objects.filter(
                tenant_id=request.user.tenant_id,
                direction="client_receivable",
                status__in=["open", "partial"],
                due_date__lt=today,
            ).count()
            finance_summary = {
                "receivable": [money_dict(r["total"], r["currency"]) for r in receivable],
                "overdue_obligations": overdue,
            }

        incidents = IntegrationIncident.objects.filter(
            tenant_id=request.user.tenant_id,
            status__in=["open", "assigned", "reopened", "escalated"],
        )

        from services.models import OrderService

        upcoming_deadlines = OrderService.objects.filter(
            tenant_id=request.user.tenant_id,
            ticketing_deadline__gt=now,
            ticketing_deadline__lte=now + timedelta(hours=24),
            status__in=["booked", "confirmed"],
        ).select_related("order", "supplier")

        recent_orders = orders.order_by("-created_at")[:20]
        warnings = [
            {
                "type": "integration_incident",
                "severity": incident.severity,
                "title": incident.error_code,
                "description": incident.sanitized_error,
                "created_at": incident.created_at,
                "resource_id": str(incident.id),
            }
            for incident in incidents.order_by("-created_at")[:20]
        ]
        recent_activity = [
            {
                "type": "order",
                "title": order.number,
                "description": order.purpose,
                "created_at": order.updated_at,
                "order": str(order.id),
                "order_number": order.number,
            }
            for order in orders.order_by("-updated_at")[:20]
        ]

        def order_client_name(order):
            if order.client_company:
                return order.client_company.short_name or order.client_company.legal_name
            if order.client_person:
                return order.client_person.full_name
            return ""

        return Response(
            {
                "calculated_at": now,
                "orders": {
                    "total": orders.count(),
                    "by_status": status_breakdown,
                    "new_today": orders.filter(created_at__date=today).count(),
                },
                "kpi": {
                    "orders_total": orders.count(),
                    "orders_new_today": orders.filter(created_at__date=today).count(),
                    "orders_active": orders.filter(status__in=active_statuses).count(),
                    "tasks_open": len(my_tasks),
                    "sla_open": sla.count(),
                    "sla_breached": sla_breached,
                    "trips_today": trips_today.count(),
                    "integration_incidents_open": incidents.count(),
                },
                "recent_orders": [
                    {
                        "id": str(order.id),
                        "number": order.number,
                        "status": order.status,
                        "stage": order.stage,
                        "priority": order.priority,
                        "client": order_client_name(order),
                        "operator": order.operator.get_full_name() if order.operator else "",
                        "created_at": order.created_at,
                        "planned_start": order.planned_start,
                    }
                    for order in recent_orders
                ],
                "trips_today": [
                    {
                        "id": str(t.id),
                        "order_number": t.order.number,
                        "title": t.title,
                        "starts_at": t.starts_at,
                        "criticality": t.criticality,
                    }
                    for t in trips_today[:20]
                ],
                "my_tasks": [
                    {
                        "id": str(t.id),
                        "title": t.title,
                        "due_at": t.due_at,
                        "priority": t.priority,
                        "order": str(t.order_id),
                    }
                    for t in my_tasks
                ],
                "sla": {"open": sla.count(), "breached": sla_breached},
                "finance": finance_summary,
                "integration_incidents": {
                    "open": incidents.count(),
                    "critical": incidents.filter(severity="critical").count(),
                },
                "attention": [
                    {
                        "type": "ticketing_deadline",
                        "service": str(s.id),
                        "order_number": s.order.number,
                        "deadline": s.ticketing_deadline,
                        "title": s.title,
                        "supplier": s.supplier.name if s.supplier else "",
                    }
                    for s in upcoming_deadlines[:20]
                ],
                "deadlines": [
                    {
                        "type": "ticketing_deadline",
                        "service": str(s.id),
                        "order": str(s.order_id),
                        "order_number": s.order.number,
                        "title": s.title,
                        "deadline": s.ticketing_deadline,
                        "status": s.status,
                    }
                    for s in upcoming_deadlines[:20]
                ],
                "warnings": warnings,
                "recent_activity": recent_activity,
            }
        )
