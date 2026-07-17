from django.db.models import Q, QuerySet

from common.errors import ApiError
from orders.models import Order

ELEVATED_ORDER_ROLES = {"admin", "accountant", "manager"}
ORDERING_FIELDS = {
    "created_at",
    "-created_at",
    "number",
    "-number",
    "planned_start",
    "-planned_start",
    "priority",
    "-priority",
}


def orders_visible_to(user) -> QuerySet[Order]:
    queryset = Order.objects.filter(tenant_id=user.tenant_id)
    role_codes = {assignment.role.code for assignment in user.user_roles.select_related("role")}
    if user.is_superuser or role_codes & ELEVATED_ORDER_ROLES:
        return queryset
    return queryset.filter(Q(operator=user) | Q(created_by=user))


def get_order_or_404(user, order_id) -> Order:
    order = orders_visible_to(user).filter(pk=order_id).first()
    if order is None:
        raise ApiError(code="NOT_FOUND", message="Заказ не найден", status_code=404)
    return order


def filter_orders(queryset: QuerySet[Order], params) -> QuerySet[Order]:
    if query := params.get("q", "").strip():
        queryset = queryset.filter(
            Q(number__icontains=query)
            | Q(purpose__icontains=query)
            | Q(client_person__surname__icontains=query)
            | Q(client_company__legal_name__icontains=query)
        )
    if number := params.get("number"):
        queryset = queryset.filter(number=number)
    if statuses := params.getlist("status"):
        queryset = queryset.filter(status__in=statuses)
    if request_types := params.getlist("request_type"):
        queryset = queryset.filter(request_type__in=request_types)
    if service_kinds := params.getlist("service_kind"):
        queryset = queryset.filter(services__kind__in=service_kinds).distinct()
    if operator_id := params.get("operator"):
        queryset = queryset.filter(operator_id=operator_id)
    if client_id := params.get("client"):
        queryset = queryset.filter(Q(client_person_id=client_id) | Q(client_company_id=client_id))
    if created_from := params.get("created_from"):
        queryset = queryset.filter(created_at__date__gte=created_from)
    if created_to := params.get("created_to"):
        queryset = queryset.filter(created_at__date__lte=created_to)
    if planned_from := params.get("planned_from"):
        queryset = queryset.filter(planned_start__gte=planned_from)
    if planned_to := params.get("planned_to"):
        queryset = queryset.filter(planned_start__lte=planned_to)
    if params.get("is_group") in ("true", "1"):
        queryset = queryset.filter(is_group=True)
    if priority := params.get("priority"):
        queryset = queryset.filter(priority=priority)
    if ordering := params.get("ordering"):
        fields = [field for field in ordering.split(",") if field in ORDERING_FIELDS]
        if fields:
            queryset = queryset.order_by(*fields)
    return queryset
