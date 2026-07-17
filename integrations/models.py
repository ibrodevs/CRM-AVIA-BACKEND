from django.db import models


class IntegrationLog(models.Model):
    """Sanitized лог запроса к поставщику. PII и секреты не пишутся."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    correlation_id = models.CharField(max_length=64, blank=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="integration_logs",
    )
    provider_adapter = models.CharField(max_length=100)
    operation = models.CharField(max_length=100)
    request_sanitized = models.JSONField(null=True, blank=True)
    response_sanitized = models.JSONField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    result = models.CharField(max_length=16, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    raw_error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    retries = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integrations_log"
        indexes = [
            models.Index(fields=["tenant", "correlation_id"]),
            models.Index(fields=["tenant", "provider_adapter", "-created_at"]),
        ]


class IntegrationErrorCode(models.Model):
    """Каталог нормализованных кодов ошибок поставщиков (ТЗ §13)."""

    code = models.CharField(max_length=100, primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=32)
    default_severity = models.CharField(max_length=10, default="medium")
    recommended_action = models.TextField(blank=True)
    is_retry_safe = models.BooleanField(default=False)

    class Meta:
        db_table = "integrations_error_code"


class IntegrationIncident(models.Model):
    """Живой инцидент интеграции с действиями оператора (ТЗ §21.3)."""

    class Status(models.TextChoices):
        OPEN = "open"
        ASSIGNED = "assigned"
        SNOOZED = "snoozed"
        RETRYING = "retrying"
        RESOLVED = "resolved"
        REOPENED = "reopened"
        ESCALATED = "escalated"

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    error_code = models.CharField(max_length=100)
    severity = models.CharField(max_length=10, default="medium")
    provider_adapter = models.CharField(max_length=100, blank=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    operation = models.CharField(max_length=100, blank=True)
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    service = models.ForeignKey(
        "services.OrderService", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    job = models.ForeignKey(
        "common.BackgroundJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    sanitized_error = models.TextField(blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    assignee = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    snoozed_until = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    fallback_supplier = models.ForeignKey(
        "suppliers.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolution_code = models.CharField(max_length=100, blank=True)
    resolution_comment = models.TextField(blank=True)
    developer_ticket = models.CharField(max_length=255, blank=True)
    occurrences = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_incident"
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["tenant", "error_code"]),
        ]


class IncidentTimelineEntry(models.Model):
    """Immutable timeline инцидента."""

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(IntegrationIncident, on_delete=models.CASCADE, related_name="timeline")
    action = models.CharField(max_length=32)
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integrations_incident_timeline"
        ordering = ["id"]
