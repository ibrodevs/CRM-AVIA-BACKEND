"""Поездки, конфликты и календарь (ТЗ §7.4)."""
from django.db import models

from common.models import TenantModel


class Trip(TenantModel):
    """Поездка: вычисляется из дат услуг заказа, хранит lifecycle и критичность."""

    class Status(models.TextChoices):
        PLANNED = "planned"
        UPCOMING = "upcoming"
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE, related_name="trips")
    title = models.CharField(max_length=255, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PLANNED)
    criticality = models.CharField(max_length=8, default="normal")  # normal/attention/critical
    computed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "calendar_trip"
        indexes = [models.Index(fields=["tenant", "starts_at"])]


class TripConflict(models.Model):
    """Выявленный конфликт поездки (пересечения, стыковки, дедлайны, документы)."""

    id = models.BigAutoField(primary_key=True)
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="conflicts")
    kind = models.CharField(max_length=32)  # overlap/connection/payment_overdue/missing_document/schedule_change
    severity = models.CharField(max_length=8, default="warning")  # info/warning/critical
    details = models.JSONField(default=dict, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "calendar_trip_conflict"


class CalendarEvent(TenantModel):
    class Kind(models.TextChoices):
        ORDER_TRIP = "order_trip"
        REMINDER = "reminder"
        TASK = "task"
        CONTROL = "control"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        DONE = "done"
        CANCELLED = "cancelled"
        MISSED = "missed"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=63, blank=True)
    order = models.ForeignKey("orders.Order", null=True, blank=True,
                              on_delete=models.CASCADE, related_name="calendar_events")
    service = models.ForeignKey("services.OrderService", null=True, blank=True,
                                on_delete=models.CASCADE, related_name="calendar_events")
    person = models.ForeignKey("crm.Person", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    assignee = models.ForeignKey("accounts.User", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="calendar_events")
    scope = models.CharField(max_length=16, default="personal")  # personal/team/tenant
    priority = models.CharField(max_length=8, default="normal")
    notification_method = models.CharField(max_length=32, blank=True)
    recurrence_rule = models.CharField(max_length=255, blank=True)  # RFC 5545 RRULE
    criterion = models.CharField(max_length=255, blank=True)        # для control-событий
    action_on_problem = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "calendar_event"
        indexes = [
            models.Index(fields=["tenant", "starts_at"]),
            models.Index(fields=["tenant", "assignee", "status"]),
        ]


class CalendarEventOccurrence(models.Model):
    """Occurrence повторяющегося события: complete одной occurrence не завершает
    серию (ТЗ §30)."""

    id = models.BigAutoField(primary_key=True)
    event = models.ForeignKey(CalendarEvent, on_delete=models.CASCADE, related_name="occurrences")
    occurs_at = models.DateTimeField()
    status = models.CharField(max_length=10, default="scheduled")
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "calendar_event_occurrence"
        constraints = [
            models.UniqueConstraint(fields=["event", "occurs_at"], name="uniq_event_occurrence"),
        ]
