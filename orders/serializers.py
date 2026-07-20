from rest_framework import serializers

from common.money import money_dict
from crm.models import Agreement, Company, Person
from orders.models import Order, OrderParticipant, OrderTask, Route, RoutePoint


class RoutePointSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoutePoint
        fields = [
            "id",
            "sequence",
            "location_code",
            "location_type",
            "location_name",
            "local_datetime",
            "timezone",
        ]
        read_only_fields = ["id", "sequence"]


class RouteSerializer(serializers.ModelSerializer):
    points = RoutePointSerializer(many=True, read_only=True)

    class Meta:
        model = Route
        fields = ["id", "kind", "points", "version"]


class ParticipantSerializer(serializers.ModelSerializer):
    person_name = serializers.CharField(source="person.full_name", read_only=True, default="")

    class Meta:
        model = OrderParticipant
        fields = [
            "id",
            "person",
            "person_name",
            "guest_snapshot",
            "role",
            "group_name",
            "subgroup_name",
            "is_contact",
            "booking_document",
            "status",
            "notes",
        ]
        read_only_fields = ["id", "status"]


class OrderTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderTask
        fields = [
            "id",
            "title",
            "description",
            "assignee",
            "due_at",
            "priority",
            "status",
            "completed_at",
            "created_at",
        ]
        read_only_fields = ["id", "completed_at", "created_at"]


class OrderListSerializer(serializers.ModelSerializer):
    client_name = serializers.SerializerMethodField()
    operator_name = serializers.CharField(source="operator.get_full_name", read_only=True, default="")
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    services_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    service_kind = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "number",
            "request_type",
            "status",
            "status_display",
            "stage",
            "priority",
            "client_person",
            "client_company",
            "client_name",
            "operator",
            "operator_name",
            "planned_start",
            "planned_end",
            "base_currency",
            "services_count",
            "total_amount",
            "service_kind",
            "is_group",
            "created_at",
            "version",
        ]

    def get_client_name(self, obj) -> str:
        if obj.client_person_id:
            return obj.client_person.full_name
        if obj.client_company_id:
            return str(obj.client_company)
        return ""

    def _services(self, obj):
        return list(obj.services.all())

    def get_services_count(self, obj) -> int:
        return len(self._services(obj))

    def get_total_amount(self, obj):
        return sum((service.client_total or 0) for service in self._services(obj))

    def get_service_kind(self, obj) -> str:
        services = self._services(obj)
        return services[0].kind if services else ""


class OrderDetailSerializer(OrderListSerializer):
    route = RouteSerializer(read_only=True)
    participants = serializers.SerializerMethodField()

    class Meta(OrderListSerializer.Meta):
        fields = OrderListSerializer.Meta.fields + [
            "contact_person",
            "source",
            "preferred_channel",
            "agreement",
            "agreement_snapshot",
            "purpose",
            "comment",
            "cancelled_at",
            "cancelled_reason",
            "route",
            "participants",
            "updated_at",
        ]

    def get_participants(self, obj):
        return ParticipantSerializer(
            obj.participants.filter(status="active").select_related("person"), many=True
        ).data


class OrderCreateSerializer(serializers.Serializer):
    request_type = serializers.ChoiceField(
        choices=Order.RequestType.choices, default=Order.RequestType.INDIVIDUAL
    )
    client_person = serializers.PrimaryKeyRelatedField(
        queryset=Person.objects.all(), required=False, allow_null=True
    )
    client_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.all(), required=False, allow_null=True
    )
    contact_person = serializers.PrimaryKeyRelatedField(
        queryset=Person.objects.all(), required=False, allow_null=True
    )
    priority = serializers.ChoiceField(choices=Order.Priority.choices, default=Order.Priority.NORMAL)
    source = serializers.CharField(required=False, allow_blank=True, max_length=32)
    preferred_channel = serializers.CharField(required=False, allow_blank=True, max_length=32)
    base_currency = serializers.CharField(default="USD", max_length=3)
    agreement = serializers.PrimaryKeyRelatedField(
        queryset=Agreement.objects.all(), required=False, allow_null=True
    )
    planned_start = serializers.DateField(required=False, allow_null=True)
    planned_end = serializers.DateField(required=False, allow_null=True)
    purpose = serializers.CharField(required=False, allow_blank=True, max_length=255)
    comment = serializers.CharField(required=False, allow_blank=True)
    route = serializers.DictField(required=False, allow_null=True)
    participants = serializers.ListField(child=serializers.DictField(), required=False)

    def validate(self, attrs):
        person, company = attrs.get("client_person"), attrs.get("client_company")
        if bool(person) == bool(company):
            raise serializers.ValidationError(
                {"client_person": ["Укажите клиента: физлицо или компанию (ровно одно)"]}
            )
        route = attrs.get("route")
        if route:
            from django.conf import settings

            points = route.get("points", [])
            if len(points) < 2:
                raise serializers.ValidationError({"route": ["Маршрут содержит минимум 2 точки"]})
            if route.get("kind") == "multi_city" and len(points) > settings.MULTI_CITY_MAX_SEGMENTS + 1:
                raise serializers.ValidationError(
                    {"route": [f"Максимум {settings.MULTI_CITY_MAX_SEGMENTS} сегментов multi-city"]}
                )

        return attrs


def order_finance_summary(order: Order) -> dict:
    """Финансовая сводка заказа из услуг/ledger (агрегаты не приходят от клиента)."""
    from decimal import Decimal

    totals: dict[str, Decimal] = {}
    for service in order.services.exclude(status__in=["cancelled", "failed"]):
        if service.client_total is not None:
            totals[service.currency] = totals.get(service.currency, Decimal(0)) + service.client_total
    paid: dict[str, Decimal] = {}
    outstanding: dict[str, Decimal] = {}
    try:
        from finance.models import FinancialObligation

        for obligation in FinancialObligation.objects.filter(order=order, direction="client_receivable"):
            paid[obligation.currency] = paid.get(obligation.currency, Decimal(0)) + obligation.paid_amount
            outstanding[obligation.currency] = (
                outstanding.get(obligation.currency, Decimal(0)) + obligation.outstanding_amount
            )
    except Exception:
        pass
    return {
        "services_total": [money_dict(v, k) for k, v in totals.items()],
        "paid": [money_dict(v, k) for k, v in paid.items()],
        "outstanding": [money_dict(v, k) for k, v in outstanding.items()],
        "services_count": order.services.count(),
    }
