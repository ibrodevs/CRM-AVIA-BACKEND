"""Глобальный поиск (ТЗ §20). Права применяются до формирования результата."""
from django.db.models import Q
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import has_permission
from common.errors import ApiError

DEFAULT_TYPES = ["order", "person", "company", "supplier"]


class SearchThrottle(UserRateThrottle):
    scope = "search"


class GlobalSearchView(APIView):
    throttle_classes = [SearchThrottle]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        if len(q) < 2:
            raise ApiError(code="QUERY_TOO_SHORT", message="Минимум 2 символа",
                           status_code=400)
        limit = min(int(request.query_params.get("limit", 20)), 50)
        types = [t for t in request.query_params.get("types", "").split(",") if t] or DEFAULT_TYPES
        user = request.user
        results: list[dict] = []

        if "order" in types and has_permission(user, "orders.view"):
            from orders.views import orders_visible_to

            for order in orders_visible_to(user).filter(
                Q(number__icontains=q) | Q(purpose__icontains=q)
            ).select_related("client_person", "client_company")[:limit]:
                client = (order.client_person.full_name if order.client_person_id
                          else str(order.client_company) if order.client_company_id else "")
                results.append({
                    "type": "order", "id": str(order.id), "title": order.number,
                    "subtitle": client, "deep_link": f"/orders/{order.id}",
                    "score": 1.0 if order.number.lower() == q.lower() else 0.5,
                })

        if "person" in types and has_permission(user, "crm.view"):
            from crm.models import Person

            for person in Person.objects.filter(
                tenant_id=user.tenant_id, archived_at__isnull=True
            ).filter(
                Q(surname__icontains=q) | Q(given_name__icontains=q)
                | Q(latin_surname__icontains=q) | Q(latin_given_name__icontains=q)
                | Q(phone__icontains=q) | Q(email__icontains=q)
            )[:limit]:
                results.append({
                    "type": "person", "id": str(person.id), "title": person.full_name,
                    "subtitle": person.phone or person.email,
                    "deep_link": f"/persons/{person.id}", "score": 0.5,
                })

        if "company" in types and has_permission(user, "crm.view"):
            from crm.models import Company

            for company in Company.objects.filter(
                tenant_id=user.tenant_id, archived_at__isnull=True
            ).filter(Q(legal_name__icontains=q) | Q(short_name__icontains=q)
                     | Q(tax_id__icontains=q))[:limit]:
                results.append({
                    "type": "company", "id": str(company.id), "title": str(company),
                    "subtitle": company.tax_id, "deep_link": f"/companies/{company.id}",
                    "score": 0.5,
                })

        if "supplier" in types and has_permission(user, "suppliers.view"):
            from suppliers.models import Supplier

            for supplier in Supplier.objects.filter(
                tenant_id=user.tenant_id, archived_at__isnull=True
            ).filter(Q(name__icontains=q) | Q(legal_name__icontains=q))[:limit]:
                results.append({
                    "type": "supplier", "id": str(supplier.id), "title": supplier.name,
                    "subtitle": supplier.organization_type,
                    "deep_link": f"/suppliers/{supplier.id}", "score": 0.5,
                })

        results.sort(key=lambda r: -r["score"])
        return Response({"query": q, "results": results[:limit]})
