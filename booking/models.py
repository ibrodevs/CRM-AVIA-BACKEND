from django.db import models

from common.models import TenantModel


class BookingWorkflow(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PREFLIGHT_OK = "preflight_ok"
        RUNNING = "running"
        COMPLETED = "completed"
        PARTIAL = "partial"
        FAILED = "failed"
        CANCELLED = "cancelled"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="booking_workflows")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.DRAFT)
    plan_snapshot = models.JSONField(null=True, blank=True)
    preflight_result = models.JSONField(null=True, blank=True)
    preflight_at = models.DateTimeField(null=True, blank=True)
    price_confirmation_required = models.BooleanField(default=False)
    prices_confirmed_at = models.DateTimeField(null=True, blank=True)
    job = models.ForeignKey(
        "common.BackgroundJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "booking_workflow"
        indexes = [models.Index(fields=["tenant", "order", "status"])]


class BookingWorkflowItem(TenantModel):
    """Услуга в составе workflow: каждый внешний результат записывается (ТЗ §10)."""

    class Status(models.TextChoices):
        PENDING = "pending"
        BOOKING = "booking"
        BOOKED = "booked"
        ISSUING = "issuing"
        ISSUED = "issued"
        FAILED = "failed"
        UNKNOWN = "unknown"
        COMPENSATED = "compensated"
        SKIPPED = "skipped"

    workflow = models.ForeignKey(BookingWorkflow, on_delete=models.CASCADE, related_name="items")
    service = models.ForeignKey(
        "services.OrderService", on_delete=models.PROTECT, related_name="workflow_items"
    )
    sequence = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    locator = models.CharField(max_length=32, blank=True)
    provider_result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "booking_workflow_item"
        constraints = [
            models.UniqueConstraint(fields=["workflow", "service"], name="uniq_workflow_service"),
        ]
        ordering = ["sequence"]
