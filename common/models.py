"""Базовые модели и инфраструктурные таблицы (ТЗ §3.3, §3.4, §21.4, §22)."""
import uuid

from django.conf import settings
from django.db import models

from tenancy.context import get_current_tenant_id


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant_id):
        return self.filter(tenant_id=tenant_id)

    def active(self):
        return self.filter(archived_at__isnull=True)


class TenantManager(models.Manager):
    """Автоматически ограничивает queryset текущим tenant-контекстом.

    Если контекст не установлен (management commands, миграции) — не фильтрует;
    фоновые задания обязаны устанавливать tenant_context явно.
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db)
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


class TenantModel(models.Model):
    """Общая база бизнес-моделей: UUID PK, tenant, аудит-поля, optimistic locking,
    archived_at вместо hard delete."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", editable=False,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", editable=False,
    )
    version = models.PositiveIntegerField(default=1)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            tenant_id = get_current_tenant_id()
            if tenant_id is not None:
                self.tenant_id = tenant_id
        super().save(*args, **kwargs)


class IdempotencyRecord(models.Model):
    """Ключи идемпотентности команд (ТЗ §3.4).

    Один ключ в пределах tenant + principal + endpoint возвращает первоначальный
    результат; повтор с другим телом — 409 IDEMPOTENCY_CONFLICT.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    endpoint = models.CharField(max_length=255)
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(
        max_length=12,
        choices=[("in_progress", "in_progress"), ("completed", "completed")],
        default="in_progress",
    )
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "common_idempotency_record"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user", "endpoint", "key"], name="uniq_idempotency_key"
            ),
        ]
        indexes = [models.Index(fields=["created_at"])]


class AuditEvent(models.Model):
    """Append-only аудит (ТЗ §21.4). Изменение/удаление через API запрещено."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    impersonator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=100)  # напр. "order.status_changed", "auth.login_failed"
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)
    request_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    reason = models.TextField(blank=True)
    before = models.JSONField(null=True, blank=True)  # redacted diff
    after = models.JSONField(null=True, blank=True)   # redacted diff

    class Meta:
        db_table = "common_audit_event"
        indexes = [
            models.Index(fields=["tenant", "resource_type", "resource_id", "-occurred_at"]),
            models.Index(fields=["tenant", "actor", "-occurred_at"]),
            models.Index(fields=["tenant", "action", "-occurred_at"]),
        ]


class OutboxEvent(models.Model):
    """Transactional outbox + событийный feed для cursor polling (ТЗ §4.4, §22).

    BigAutoField id служит монотонным cursor-ом. Domain event записывается
    в одной транзакции с изменением данных; run_jobs обрабатывает side effects.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    event_type = models.CharField(max_length=100)  # напр. "order.updated"
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=64)
    resource_version = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)  # минимальный payload, не источник истины
    audience_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
        help_text="Если задан — событие видно только этому пользователю",
    )
    occurred_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)  # обработка side effects
    process_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "common_outbox_event"
        indexes = [
            models.Index(fields=["tenant", "id"]),
            models.Index(
                fields=["id"], name="idx_outbox_unprocessed", condition=models.Q(processed_at__isnull=True)
            ),
            models.Index(fields=["occurred_at"]),
        ]


class BackgroundJob(models.Model):
    """Фоновое задание в PostgreSQL-очереди (ТЗ §22). Без Redis/Celery.

    Забирается run_jobs через SELECT ... FOR UPDATE SKIP LOCKED.
    """

    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        CANCELLED = "cancelled"
        DEAD = "dead"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    kind = models.CharField(max_length=100)  # ключ в реестре обработчиков
    payload = models.JSONField(default=dict, blank=True)  # с redaction, без секретов
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    priority = models.SmallIntegerField(default=100)  # меньше = раньше
    progress = models.PositiveSmallIntegerField(default=0)  # 0..100
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    run_after = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    result = models.JSONField(null=True, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "common_background_job"
        indexes = [
            models.Index(
                fields=["priority", "created_at"],
                name="idx_job_queue",
                condition=models.Q(status="queued"),
            ),
            models.Index(fields=["tenant", "kind", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} [{self.status}]"
