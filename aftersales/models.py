from django.db import models

from common.models import TenantModel


class AfterSaleNumberCounter(models.Model):
    tenant = models.OneToOneField(
        "tenancy.Organization", primary_key=True, on_delete=models.CASCADE, related_name="+"
    )
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "aftersales_number_counter"

    @classmethod
    def next_number(cls, tenant_id) -> str:
        counter, _ = cls.objects.get_or_create(tenant_id=tenant_id)
        counter = cls.objects.select_for_update().get(pk=counter.pk)
        counter.last_value += 1
        counter.save(update_fields=["last_value"])
        return f"AS-{counter.last_value:06d}"


class AfterSaleCase(TenantModel):
    class Kind(models.TextChoices):
        REFUND = "refund"
        EXCHANGE = "exchange"
        CANCELLATION = "cancellation"
        CERTIFICATE = "certificate"

    class Status(models.TextChoices):
        CREATED = "created"
        REVIEW = "review"
        AWAITING_CLIENT_APPROVAL = "awaiting_client_approval"
        SUBMITTED_TO_SUPPLIER = "submitted_to_supplier"
        PROCESSING = "processing"
        COMPLETED = "completed"
        CANCELLED = "cancelled"
        REJECTED = "rejected"

    number = models.CharField(max_length=20)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="aftersale_cases")
    service = models.ForeignKey(
        "services.OrderService",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="aftersale_cases",
    )
    type = models.CharField(max_length=14, choices=Kind.choices)
    initiator = models.CharField(max_length=16, default="client")
    responsible = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="aftersale_cases"
    )
    participants = models.ManyToManyField(
        "orders.OrderParticipant", blank=True, related_name="aftersale_cases"
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="aftersale_cases"
    )
    status = models.CharField(max_length=26, choices=Status.choices, default=Status.CREATED)
    deadline = models.DateTimeField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    financial_snapshot = models.JSONField(null=True, blank=True)
    external_references = models.JSONField(default=dict, blank=True)
    current_quote = models.ForeignKey(
        "aftersales.AfterSaleQuote", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    client_approved_at = models.DateTimeField(null=True, blank=True)
    client_approved_quote_version = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "aftersales_case"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_aftersale_number"),
        ]
        indexes = [models.Index(fields=["tenant", "status", "deadline"])]


AFTERSALE_TRANSITIONS: dict[str, set[str]] = {
    AfterSaleCase.Status.CREATED: {AfterSaleCase.Status.REVIEW, AfterSaleCase.Status.CANCELLED},
    AfterSaleCase.Status.REVIEW: {
        AfterSaleCase.Status.AWAITING_CLIENT_APPROVAL,
        AfterSaleCase.Status.SUBMITTED_TO_SUPPLIER,
        AfterSaleCase.Status.CANCELLED,
        AfterSaleCase.Status.REJECTED,
    },
    AfterSaleCase.Status.AWAITING_CLIENT_APPROVAL: {
        AfterSaleCase.Status.SUBMITTED_TO_SUPPLIER,
        AfterSaleCase.Status.REVIEW,
        AfterSaleCase.Status.CANCELLED,
        AfterSaleCase.Status.REJECTED,
    },
    AfterSaleCase.Status.SUBMITTED_TO_SUPPLIER: {
        AfterSaleCase.Status.PROCESSING,
        AfterSaleCase.Status.REJECTED,
        AfterSaleCase.Status.CANCELLED,
    },
    AfterSaleCase.Status.PROCESSING: {AfterSaleCase.Status.COMPLETED, AfterSaleCase.Status.REJECTED},
    AfterSaleCase.Status.COMPLETED: set(),
    AfterSaleCase.Status.CANCELLED: set(),
    AfterSaleCase.Status.REJECTED: set(),
}


class AfterSaleQuote(TenantModel):
    """Версионируемый расчёт возврата/обмена. Новая версия делает старое
    согласие клиента недействительным (ТЗ §16)."""

    case = models.ForeignKey(AfterSaleCase, on_delete=models.CASCADE, related_name="quotes")
    quote_version = models.PositiveIntegerField()
    source = models.CharField(max_length=16, default="manual")
    currency = models.CharField(max_length=3)
    original_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    supplier_penalty = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    agency_service_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_withholdings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    refund_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    old_itinerary = models.JSONField(null=True, blank=True)
    new_itinerary = models.JSONField(null=True, blank=True)
    exchange_difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "aftersales_quote"
        constraints = [
            models.UniqueConstraint(fields=["case", "quote_version"], name="uniq_case_quote_version"),
        ]


class AfterSaleHistoryEntry(models.Model):
    """Полная история кейса: письма, ответы, решения (ТЗ §16)."""

    id = models.BigAutoField(primary_key=True)
    case = models.ForeignKey(AfterSaleCase, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=48)
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "aftersales_history"
        ordering = ["id"]
