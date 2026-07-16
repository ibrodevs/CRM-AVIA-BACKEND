"""Смены, SLA, мотивация (ТЗ §19)."""
from django.db import models

from common.models import TenantModel


class SlaPolicy(TenantModel):
    event_type = models.CharField(max_length=64)  # new_order/client_message/...
    service_kind = models.CharField(max_length=16, blank=True)
    priority = models.CharField(max_length=8, blank=True)
    response_minutes = models.PositiveIntegerField()
    resolution_minutes = models.PositiveIntegerField(null=True, blank=True)
    work_calendar = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "workforce_sla_policy"


class SlaInstance(TenantModel):
    policy = models.ForeignKey(SlaPolicy, on_delete=models.PROTECT, related_name="instances")
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=64)
    assignee = models.ForeignKey("accounts.User", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="sla_instances")
    started_at = models.DateTimeField()
    paused_intervals = models.JSONField(default=list, blank=True)
    response_deadline = models.DateTimeField(null=True, blank=True)
    resolution_deadline = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    breached_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "workforce_sla_instance"
        indexes = [models.Index(fields=["tenant", "assignee", "response_deadline"])]


class Shift(TenantModel):
    """Не более одной открытой смены на пользователя (ТЗ §30)."""

    class Status(models.TextChoices):
        OPEN = "open"
        CLOSED = "closed"

    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="shifts")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2,
                                          null=True, blank=True)
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2,
                                          null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    closing_report = models.JSONField(null=True, blank=True)  # immutable snapshot
    discrepancy_confirmed = models.BooleanField(default=False)

    class Meta:
        db_table = "workforce_shift"
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=models.Q(status="open"),
                                    name="uniq_open_shift_per_user"),
        ]


class ShiftOperation(models.Model):
    id = models.BigAutoField(primary_key=True)
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="operations")
    kind = models.CharField(max_length=32)  # payment/order_created/issue/...
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workforce_shift_operation"


class MotivationRule(TenantModel):
    service_kind = models.CharField(max_length=16, default="*")
    fee_percent = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    markup_percent = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    commission_percent = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "workforce_motivation_rule"


class MotivationAccrual(TenantModel):
    """Начисление по факту признанного дохода; может быть reversed (ТЗ §19)."""

    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT,
                             related_name="motivation_accruals")
    rule = models.ForeignKey(MotivationRule, on_delete=models.PROTECT, related_name="+")
    service = models.ForeignKey("services.OrderService", on_delete=models.PROTECT,
                                related_name="motivation_accruals")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_of = models.OneToOneField("self", null=True, blank=True,
                                       on_delete=models.PROTECT, related_name="reversed_by")

    class Meta:
        db_table = "workforce_motivation_accrual"
