import pytest
from django.core.management import call_command
from django.utils import timezone

from common.jobs import JobRetry, enqueue, job_handler
from common.models import BackgroundJob, OutboxEvent
from common.outbox import emit_event
from common.outbox_processors import outbox_processor

pytestmark = pytest.mark.django_db

_executed: list[str] = []


@job_handler("test.ok")
def _ok_handler(job):
    _executed.append(str(job.id))
    return {"echo": job.payload.get("value")}


@job_handler("test.retry", max_attempts=2, retryable=True)
def _retry_handler(job):  # noqa: ARG001
    raise JobRetry("try later", delay_seconds=60)


@job_handler("test.boom", max_attempts=1)
def _boom_handler(job):  # noqa: ARG001
    raise RuntimeError("boom")


_processed_events: list[str] = []


@outbox_processor("test.*")
def _test_processor(event):
    _processed_events.append(event.event_type)


def run_once():
    call_command("run_jobs", "--once", "--worker-id", "test-worker")


class TestJobRunner:
    def test_success(self, tenant):
        job = enqueue("test.ok", {"value": 42}, tenant_id=tenant.id)
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.SUCCEEDED
        assert job.result == {"echo": 42}
        assert job.progress == 100

    def test_retry_then_dead(self, tenant):
        job = enqueue("test.retry", tenant_id=tenant.id)
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.QUEUED
        assert job.run_after > timezone.now()

        job.run_after = timezone.now()
        job.save(update_fields=["run_after"])
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.DEAD

    def test_failure_no_retry(self, tenant):
        job = enqueue("test.boom", tenant_id=tenant.id)
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.DEAD
        assert job.error_code == "RUNTIMEERROR"

    def test_unknown_kind(self, tenant):
        job = enqueue("test.unknown_kind", tenant_id=tenant.id)
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.FAILED
        assert job.error_code == "UNKNOWN_JOB_KIND"

    def test_run_after_respected(self, tenant):
        from datetime import timedelta

        job = enqueue("test.ok", run_after=timezone.now() + timedelta(hours=1), tenant_id=tenant.id)
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.QUEUED

    def test_stale_job_requeued(self, tenant):
        from datetime import timedelta

        job = enqueue("test.ok", tenant_id=tenant.id)
        BackgroundJob.objects.filter(pk=job.pk).update(
            status=BackgroundJob.Status.RUNNING,
            locked_by="dead-worker",
            heartbeat_at=timezone.now() - timedelta(hours=1),
        )
        run_once()
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.SUCCEEDED

    def test_outbox_processing(self, tenant):
        emit_event("test.created", "Thing", tenant_id=tenant.id)
        run_once()
        event = OutboxEvent.objects.get(event_type="test.created")
        assert event.processed_at is not None
        assert "test.created" in _processed_events


class TestJobApi:
    def test_job_status_endpoint(self, admin_user, tenant):
        from conftest import auth_client

        job = enqueue("test.ok", {"value": 1}, tenant_id=tenant.id)
        client = auth_client(admin_user)
        body = client.get(f"/api/v1/jobs/{job.id}/").json()
        assert body["status"] == "queued"

    def test_cancel_queued_job(self, admin_user, tenant):
        from conftest import auth_client

        job = enqueue("test.ok", tenant_id=tenant.id)
        client = auth_client(admin_user)
        response = client.post(f"/api/v1/jobs/{job.id}/cancel/")
        assert response.status_code == 200
        job.refresh_from_db()
        assert job.status == BackgroundJob.Status.CANCELLED

    def test_foreign_tenant_job_not_visible(self, admin_user, other_tenant):
        from conftest import auth_client

        job = enqueue("test.ok", tenant_id=other_tenant.id)
        client = auth_client(admin_user)
        assert client.get(f"/api/v1/jobs/{job.id}/").status_code == 404


class TestScheduled:
    def test_run_scheduled_jobs_cleanup(self, tenant):
        from datetime import timedelta

        old = emit_event("test.old", "Thing", tenant_id=tenant.id)
        OutboxEvent.objects.filter(pk=old.pk).update(
            occurred_at=timezone.now() - timedelta(days=30), processed_at=timezone.now()
        )
        call_command("run_scheduled_jobs", "--only", "common.cleanup_events")
        assert not OutboxEvent.objects.filter(pk=old.pk).exists()
