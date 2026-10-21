from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.jobs import job_handler
from integrations.models import IntegrationIncident
from integrations.scheduled_tasks import needs_sync, sync_delay


@job_handler("integrations.hotelbook_status_sync", max_attempts=3, retryable=True, user_cancellable=False)
@transaction.atomic
def sync_hotelbook_status(job):
    from booking.job_handlers import status_inquiry
    from booking.models import BookingWorkflowItem
    from services.models import OrderService

    service = OrderService.objects.select_for_update().get(
        pk=job.payload["service_id"], tenant_id=job.tenant_id
    )
    booking = (service.provider_snapshot or {}).get("hotelbook_booking", {})
    sync = booking.get("sync", {})
    due = parse_datetime(sync.get("next_at", ""))
    if not needs_sync(booking) or sync.get("exhausted") or (due and due > timezone.now()):
        return {"status": "skipped"}
    item = (
        BookingWorkflowItem.objects.filter(service=service)
        .exclude(status__in=["failed", "skipped"])
        .order_by("-created_at")
        .first()
    )
    if item is None:
        return {"status": "no_workflow"}
    original_payload = job.payload
    try:
        job.payload = {"item_id": str(item.id)}
        outcome = status_inquiry(job)
    finally:
        job.payload = original_payload
    service.refresh_from_db()
    snapshot = service.provider_snapshot
    booking = snapshot["hotelbook_booking"]
    attempt = sync.get("attempts", 0) + 1
    exhausted = attempt >= 100
    booking["sync"] = {
        "attempts": attempt,
        "last_at": timezone.now().isoformat(),
        "next_at": (timezone.now() + sync_delay(attempt - 1)).isoformat(),
        "exhausted": exhausted,
        "last_error": outcome.get("error_code", ""),
    }
    service.provider_snapshot = snapshot
    service.save(update_fields=["provider_snapshot", "updated_at"])
    if exhausted and needs_sync(booking):
        IntegrationIncident.objects.get_or_create(
            tenant_id=service.tenant_id,
            service=service,
            error_code="HBPRO_SYNC_EXHAUSTED",
            status="open",
            defaults={
                "supplier": service.supplier,
                "order": service.order,
                "provider_adapter": "hotelbook",
                "operation": "status_sync",
                "sanitized_error": "Лимит автоматической сверки исчерпан; обновите статус вручную",
            },
        )
    return {**outcome, "attempt": attempt, "exhausted": exhausted}
