"""Периодические задачи обслуживания: retention событий, ключей идемпотентности
и завершённых технических заданий. Бизнес-аудит (AuditEvent) не очищается.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from common.models import BackgroundJob, IdempotencyRecord, OutboxEvent
from common.scheduled import scheduled_task


@scheduled_task("common.cleanup_events")
def cleanup_events() -> str:
    cutoff = timezone.now() - timedelta(days=settings.EVENT_RETENTION_DAYS)
    deleted, _ = OutboxEvent.objects.filter(occurred_at__lt=cutoff, processed_at__isnull=False).delete()
    return f"deleted {deleted} outbox events"


@scheduled_task("common.cleanup_idempotency")
def cleanup_idempotency() -> str:
    cutoff = timezone.now() - timedelta(days=settings.IDEMPOTENCY_RETENTION_DAYS)
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
    return f"deleted {deleted} idempotency records"


@scheduled_task("common.cleanup_finished_jobs")
def cleanup_finished_jobs() -> str:
    cutoff = timezone.now() - timedelta(days=30)
    deleted, _ = BackgroundJob.objects.filter(
        status__in=[BackgroundJob.Status.SUCCEEDED, BackgroundJob.Status.CANCELLED],
        completed_at__lt=cutoff,
    ).delete()
    return f"deleted {deleted} finished jobs"
