from django.db import models, transaction

from common.models import TenantModel


class OrderNumberCounter(models.Model):
    """Счётчик номеров заказов под транзакционной блокировкой (ТЗ §2.1: не max+1)."""

    tenant = models.OneToOneField(
        "tenancy.Organization", primary_key=True, on_delete=models.CASCADE, related_name="+"
    )
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "orders_number_counter"

    @classmethod
    def next_number(cls, tenant_id) -> str:
        """Атомарно выдаёт следующий номер. Вызывать внутри transaction.atomic()."""
        counter, _ = cls.objects.get_or_create(tenant_id=tenant_id)
        counter = cls.objects.select_for_update().get(pk=counter.pk)
        counter.last_value += 1
        counter.save(update_fields=["last_value"])
        return f"ORD-{counter.last_value:06d}"


class Order(TenantModel):
    class RequestType(models.TextChoices):
        INDIVIDUAL = "individual"
        GROUP = "group"
        CORPORATE = "corporate"

    class Status(models.TextChoices):
        NEW = "new"
        IN_PROGRESS = "in_progress"
        AWAITING_CONFIRMATION = "awaiting_confirmation"
        AWAITING_PAYMENT = "awaiting_payment"
        PAID = "paid"
        COMPLETED = "completed"
        NEEDS_REVIEW = "needs_review"
        ON_HOLD = "on_hold"
        CANCELLED = "cancelled"
        DATA_MISSING = "data_missing"

    class Stage(models.TextChoices):
        CREATED = "created"
        SERVICE_SELECTION = "service_selection"
        BOOKING = "booking"
        TICKETING = "ticketing"
        DOCUMENTS = "documents"
        COMPLETED = "completed"

    class Priority(models.TextChoices):
        LOW = "low"
        NORMAL = "normal"
        HIGH = "high"
        URGENT = "urgent"

    number = models.CharField(max_length=20)
    request_type = models.CharField(
        max_length=12, choices=RequestType.choices, default=RequestType.INDIVIDUAL
    )
    client_person = models.ForeignKey(
        "crm.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="orders"
    )
    client_company = models.ForeignKey(
        "crm.Company", null=True, blank=True, on_delete=models.PROTECT, related_name="orders"
    )
    contact_person = models.ForeignKey(
        "crm.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.NEW)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.CREATED)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    operator = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_orders"
    )
    source = models.CharField(max_length=32, blank=True)
    preferred_channel = models.CharField(max_length=32, blank=True)
    base_currency = models.CharField(max_length=3, default="USD")
    agreement = models.ForeignKey(
        "crm.Agreement", null=True, blank=True, on_delete=models.PROTECT, related_name="orders"
    )
    agreement_snapshot = models.JSONField(null=True, blank=True)
    planned_start = models.DateField(null=True, blank=True)
    planned_end = models.DateField(null=True, blank=True)
    purpose = models.CharField(max_length=255, blank=True)
    comment = models.TextField(blank=True)
    is_group = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.TextField(blank=True)

    class Meta:
        db_table = "orders_order"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_order_number"),
            models.CheckConstraint(
                condition=(
                    models.Q(client_person__isnull=False, client_company__isnull=True)
                    | models.Q(client_person__isnull=True, client_company__isnull=False)
                ),
                name="check_order_client_xor",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "operator", "status"]),
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "planned_start"]),
        ]

    def __str__(self) -> str:
        return self.number


ORDER_TRANSITIONS: dict[str, set[str]] = {
    Order.Status.NEW: {
        Order.Status.IN_PROGRESS,
        Order.Status.DATA_MISSING,
        Order.Status.CANCELLED,
        Order.Status.ON_HOLD,
    },
    Order.Status.IN_PROGRESS: {
        Order.Status.AWAITING_CONFIRMATION,
        Order.Status.NEEDS_REVIEW,
        Order.Status.DATA_MISSING,
        Order.Status.ON_HOLD,
        Order.Status.CANCELLED,
    },
    Order.Status.AWAITING_CONFIRMATION: {
        Order.Status.AWAITING_PAYMENT,
        Order.Status.IN_PROGRESS,
        Order.Status.NEEDS_REVIEW,
        Order.Status.ON_HOLD,
        Order.Status.CANCELLED,
    },
    Order.Status.AWAITING_PAYMENT: {
        Order.Status.PAID,
        Order.Status.IN_PROGRESS,
        Order.Status.ON_HOLD,
        Order.Status.CANCELLED,
    },
    Order.Status.PAID: {Order.Status.COMPLETED, Order.Status.NEEDS_REVIEW},
    Order.Status.COMPLETED: set(),
    Order.Status.NEEDS_REVIEW: {Order.Status.IN_PROGRESS, Order.Status.ON_HOLD, Order.Status.CANCELLED},
    Order.Status.ON_HOLD: {Order.Status.IN_PROGRESS, Order.Status.CANCELLED},
    Order.Status.CANCELLED: set(),
    Order.Status.DATA_MISSING: {Order.Status.IN_PROGRESS, Order.Status.CANCELLED},
}

TERMINAL_ORDER_STATUSES = {Order.Status.COMPLETED, Order.Status.CANCELLED}


class OrderParticipant(TenantModel):
    """Участник заказа: существующее лицо или snapshot гостя (ТЗ §7.1)."""

    class Role(models.TextChoices):
        PASSENGER = "passenger"
        CONTACT = "contact"
        APPROVER = "approver"
        OTHER = "other"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="participants")
    person = models.ForeignKey(
        "crm.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="participations"
    )
    guest_snapshot = models.JSONField(null=True, blank=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.PASSENGER)
    group_name = models.CharField(max_length=100, blank=True)
    subgroup_name = models.CharField(max_length=100, blank=True)
    is_contact = models.BooleanField(default=False)
    booking_document = models.ForeignKey(
        "crm.PersonDocument", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=16, default="active")
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "orders_participant"
        constraints = [
            models.UniqueConstraint(
                fields=["order", "person"],
                condition=models.Q(status="active") & models.Q(person__isnull=False),
                name="uniq_active_participant",
            ),
            models.CheckConstraint(
                condition=models.Q(person__isnull=False) | models.Q(guest_snapshot__isnull=False),
                name="check_participant_person_or_guest",
            ),
        ]


class Route(TenantModel):
    class Kind(models.TextChoices):
        ONE_WAY = "one_way"
        ROUND_TRIP = "round_trip"
        MULTI_CITY = "multi_city"

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="route")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ONE_WAY)

    class Meta:
        db_table = "orders_route"


class RoutePoint(TenantModel):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="points")
    sequence = models.PositiveSmallIntegerField()
    location_code = models.CharField(max_length=8)
    location_type = models.CharField(max_length=16, default="city")
    location_name = models.CharField(max_length=150, blank=True)
    local_datetime = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=63, blank=True)

    class Meta:
        db_table = "orders_route_point"
        constraints = [
            models.UniqueConstraint(fields=["route", "sequence"], name="uniq_route_point_sequence"),
        ]
        ordering = ["sequence"]


class OrderStatusHistory(models.Model):
    """Append-only история статусов заказа (ТЗ §7.2)."""

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24)
    from_stage = models.CharField(max_length=20, blank=True)
    to_stage = models.CharField(max_length=20, blank=True)
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "orders_status_history"
        ordering = ["-changed_at"]


class OrderReassignment(models.Model):
    """Фиксация переназначения ответственного (ТЗ §5.3)."""

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="reassignments")
    previous_operator = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    new_operator = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(blank=True)
    reassigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reassigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "orders_reassignment"


class OrderTask(TenantModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="order_tasks"
    )
    due_at = models.DateTimeField(null=True, blank=True)
    priority = models.CharField(max_length=8, default="normal")
    status = models.CharField(max_length=16, default="open")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "orders_task"
        indexes = [models.Index(fields=["tenant", "assignee", "status"])]


def allocate_order_number(tenant_id) -> str:
    """Выдаёт уникальный номер заказа; использовать только внутри transaction.atomic()."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("allocate_order_number требует transaction.atomic()")
    return OrderNumberCounter.next_number(tenant_id)
