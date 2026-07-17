import logging
import signal
import socket
import time
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from common.jobs import JobRetry, get_handler
from common.models import BackgroundJob, OutboxEvent
from common.outbox_processors import processors_for
from tenancy.context import tenant_context

logger = logging.getLogger("travelhub.jobs")


class Command(BaseCommand):
    help = "Запускает воркер фоновых заданий (PostgreSQL queue) и обработку outbox"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Один проход (для тестов)")
        parser.add_argument("--worker-id", default=None)

    def handle(self, *args, **options):
        from django.conf import settings

        self.cfg = settings.JOB_RUNNER
        self.worker_id = options["worker_id"] or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.stopping = False
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        logger.info("job runner started", extra={"worker_id": self.worker_id})
        while not self.stopping:
            did_work = self.run_pass()
            if options["once"]:
                break
            if not did_work:
                time.sleep(self.cfg["POLL_INTERVAL_SECONDS"])
        logger.info("job runner stopped", extra={"worker_id": self.worker_id})

    def _stop(self, *args):  # noqa: ARG002
        self.stopping = True

    def run_pass(self) -> bool:
        self.requeue_stale()
        claimed = self.claim_jobs()
        for job in claimed:
            if self.stopping:
                self.release(job)
                continue
            self.execute_job(job)
        outbox_done = self.process_outbox()
        return bool(claimed) or outbox_done

    def claim_jobs(self) -> list[BackgroundJob]:
        now = timezone.now()
        with transaction.atomic():
            jobs = list(
                BackgroundJob.objects.select_for_update(skip_locked=True)
                .filter(status=BackgroundJob.Status.QUEUED)
                .filter(Q(run_after__isnull=True) | Q(run_after__lte=now))
                .order_by("priority", "created_at")[: self.cfg["BATCH_SIZE"]]
            )
            for job in jobs:
                job.status = BackgroundJob.Status.RUNNING
                job.locked_by = self.worker_id
                job.locked_at = now
                job.heartbeat_at = now
                job.started_at = job.started_at or now
                job.attempts = F("attempts") + 1
                job.save(
                    update_fields=[
                        "status",
                        "locked_by",
                        "locked_at",
                        "heartbeat_at",
                        "started_at",
                        "attempts",
                    ]
                )
        for job in jobs:
            job.refresh_from_db()
        return jobs

    def requeue_stale(self) -> None:
        """Возвращает в очередь задания с протухшим heartbeat (умерший воркер)."""
        cutoff = timezone.now() - timedelta(seconds=self.cfg["STALE_AFTER_SECONDS"])
        with transaction.atomic():
            stale = list(
                BackgroundJob.objects.select_for_update(skip_locked=True).filter(
                    status=BackgroundJob.Status.RUNNING, heartbeat_at__lt=cutoff
                )
            )
            for job in stale:
                if job.attempts >= job.max_attempts:
                    self._mark_dead(job, "STALE_WORKER", "Воркер перестал отвечать; попытки исчерпаны")
                else:
                    job.status = BackgroundJob.Status.QUEUED
                    job.locked_by = ""
                    job.locked_at = None
                    job.save(update_fields=["status", "locked_by", "locked_at"])
                    logger.warning("stale job requeued", extra={"job_id": str(job.id), "kind": job.kind})

    def release(self, job: BackgroundJob) -> None:
        """Graceful shutdown: вернуть невыполненное задание в очередь без счёта попытки."""
        BackgroundJob.objects.filter(pk=job.pk, status=BackgroundJob.Status.RUNNING).update(
            status=BackgroundJob.Status.QUEUED,
            locked_by="",
            locked_at=None,
            attempts=F("attempts") - 1,
        )

    def execute_job(self, job: BackgroundJob) -> None:
        handler = get_handler(job.kind)
        log_extra = {
            "job_id": str(job.id),
            "kind": job.kind,
            "worker_id": self.worker_id,
            "tenant_id": str(job.tenant_id) if job.tenant_id else None,
            "attempt": job.attempts,
        }
        if handler is None:
            self._fail(
                job, "UNKNOWN_JOB_KIND", f"Обработчик '{job.kind}' не зарегистрирован", allow_retry=False
            )
            return

        started = time.monotonic()
        try:
            with tenant_context(job.tenant_id):
                result = handler.func(job)
        except JobRetry as retry:
            delay = retry.delay_seconds or min(2**job.attempts * 10, 3600)
            self._requeue(job, delay, str(retry))
            logger.info("job retry scheduled", extra={**log_extra, "delay": delay})
            return
        except Exception as exc:
            logger.exception("job failed", extra=log_extra)
            self._fail(job, type(exc).__name__.upper()[:100], str(exc)[:2000], allow_retry=handler.retryable)
            return

        updated = BackgroundJob.objects.filter(pk=job.pk, status=BackgroundJob.Status.RUNNING).update(
            status=BackgroundJob.Status.SUCCEEDED,
            progress=100,
            completed_at=timezone.now(),
            result=result,
            locked_by="",
            locked_at=None,
        )
        if updated:
            logger.info(
                "job succeeded", extra={**log_extra, "duration": round(time.monotonic() - started, 3)}
            )

    def _requeue(self, job: BackgroundJob, delay_seconds: int, message: str) -> None:
        if job.attempts >= job.max_attempts:
            self._mark_dead(job, "MAX_ATTEMPTS", message or "Попытки исчерпаны")
            return
        BackgroundJob.objects.filter(pk=job.pk).update(
            status=BackgroundJob.Status.QUEUED,
            run_after=timezone.now() + timedelta(seconds=delay_seconds),
            locked_by="",
            locked_at=None,
            error_message=message,
        )

    def _fail(self, job: BackgroundJob, code: str, message: str, *, allow_retry: bool) -> None:
        if allow_retry and job.attempts < job.max_attempts:
            delay = min(2**job.attempts * 10, 3600)
            BackgroundJob.objects.filter(pk=job.pk).update(
                status=BackgroundJob.Status.QUEUED,
                run_after=timezone.now() + timedelta(seconds=delay),
                error_code=code,
                error_message=message,
                locked_by="",
                locked_at=None,
            )
            return
        if job.attempts >= job.max_attempts:
            self._mark_dead(job, code, message)
        else:
            BackgroundJob.objects.filter(pk=job.pk).update(
                status=BackgroundJob.Status.FAILED,
                error_code=code,
                error_message=message,
                completed_at=timezone.now(),
                locked_by="",
                locked_at=None,
            )

    def _mark_dead(self, job: BackgroundJob, code: str, message: str) -> None:
        BackgroundJob.objects.filter(pk=job.pk).update(
            status=BackgroundJob.Status.DEAD,
            error_code=code,
            error_message=message,
            completed_at=timezone.now(),
            locked_by="",
            locked_at=None,
        )
        logger.error("job dead", extra={"job_id": str(job.id), "kind": job.kind, "error_code": code})
        self._create_incident(job, code, message)

    def _create_incident(self, job: BackgroundJob, code: str, message: str) -> None:
        """Создаёт IntegrationIncident для dead job (ТЗ §21.3, §22)."""
        try:
            from integrations.models import IntegrationIncident
        except Exception:
            return
        try:
            IntegrationIncident.objects.create(
                tenant_id=job.tenant_id,
                error_code=code,
                severity="high",
                operation=job.kind,
                job=job,
                sanitized_error=message[:2000],
                correlation_id=job.correlation_id,
            )
        except Exception:
            logger.exception("failed to create incident", extra={"job_id": str(job.id)})

    def process_outbox(self) -> bool:
        with transaction.atomic():
            events = list(
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(processed_at__isnull=True, process_attempts__lt=5)
                .order_by("id")[:100]
            )
            for event in events:
                try:
                    with tenant_context(event.tenant_id):
                        for processor in processors_for(event.event_type):
                            processor(event)
                    event.processed_at = timezone.now()
                    event.save(update_fields=["processed_at"])
                except Exception:
                    logger.exception(
                        "outbox processing failed",
                        extra={"event_id": event.id, "event_type": event.event_type},
                    )
                    event.process_attempts += 1
                    event.save(update_fields=["process_attempts"])
        return bool(events)
