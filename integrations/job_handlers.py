from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.jobs import job_handler
from integrations.adapters import get_adapter
from integrations.models import IntegrationIncident
from integrations.scheduled_tasks import needs_sync, sync_delay


@job_handler("integrations.hotelbook_status_sync", max_attempts=3, retryable=True, user_cancellable=False)
@job_handler("integrations.supplier_status_sync", max_attempts=3, retryable=True, user_cancellable=False)
def sync_supplier_status(job):
    from booking.job_handlers import status_inquiry
    from booking.models import BookingWorkflowItem
    from services.models import OrderService

    service = OrderService.objects.get(pk=job.payload["service_id"], tenant_id=job.tenant_id)
    provider = (service.provider_snapshot or {}).get("provider_adapter", "")
    booking_key = provider + "_booking"
    booking = (service.provider_snapshot or {}).get(booking_key, {})
    sync = booking.get("sync", {})
    due = parse_datetime(sync.get("next_at", ""))
    if not needs_sync(booking, provider) or sync.get("exhausted") or (due and due > timezone.now()):
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
    attempt = sync.get("attempts", 0) + 1
    exhausted = attempt >= 100
    with transaction.atomic():
        service = OrderService.objects.select_for_update().get(pk=service.pk)
        snapshot = service.provider_snapshot
        booking = snapshot[booking_key]
        booking["sync"] = {
            "attempts": attempt,
            "last_at": timezone.now().isoformat(),
            "next_at": (timezone.now() + sync_delay(attempt - 1)).isoformat(),
            "exhausted": exhausted,
            "last_error": outcome.get("error_code", ""),
        }
        service.provider_snapshot = snapshot
        service.save(update_fields=["provider_snapshot", "updated_at"])
    if exhausted and needs_sync(booking, provider):
        IntegrationIncident.objects.get_or_create(
            tenant_id=service.tenant_id,
            service=service,
            error_code=get_adapter(provider).sync_exhausted_code,
            status="open",
            defaults={
                "supplier": service.supplier,
                "order": service.order,
                "provider_adapter": provider,
                "operation": "status_sync",
                "sanitized_error": "Лимит автоматической сверки исчерпан; обновите статус вручную",
            },
        )
    return {**outcome, "attempt": attempt, "exhausted": exhausted}


sync_hotelbook_status = sync_supplier_status
