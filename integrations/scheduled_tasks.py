"""Queue read-only inquiries for pending supplier bookings with bounded backoff."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.jobs import enqueue
from common.models import BackgroundJob
from common.scheduled import scheduled_task
from integrations.adapters import AdapterError, get_adapter


def needs_sync(booking, provider="hotelbook"):
    try:
        return get_adapter(provider).booking_needs_sync(booking)
    except AdapterError:
        return False


@scheduled_task("integrations.hotelbook_status_sync")
@scheduled_task("integrations.supplier_status_sync")
def queue_supplier_status_sync():
    from services.models import OrderService

    queued = 0
    for service_id in (
        OrderService.all_objects.filter(
            archived_at__isnull=True,
            source="api",
            provider_snapshot__isnull=False,
        )
        .values_list("id", flat=True)
        .iterator()
    ):
        with transaction.atomic():
            service = OrderService.all_objects.select_for_update().get(pk=service_id)
            provider = (service.provider_snapshot or {}).get("provider_adapter", "")
            booking = (service.provider_snapshot or {}).get(provider + "_booking", {})
            sync = booking.get("sync", {})
            due = parse_datetime(sync.get("next_at", ""))
            if not needs_sync(booking, provider) or sync.get("exhausted") or (due and due > timezone.now()):
                continue
            if BackgroundJob.objects.filter(
                tenant_id=service.tenant_id,
                kind__in=["integrations.hotelbook_status_sync", "integrations.supplier_status_sync"],
                payload__service_id=str(service.id),
                status__in=["queued", "running"],
            ).exists():
                continue
            enqueue(
                "integrations.supplier_status_sync",
                {"service_id": str(service.id)},
                tenant_id=service.tenant_id,
            )
            queued += 1
    return f"queued {queued}"


# Existing cron/import compatibility; duplicate scheduling is suppressed above.
queue_hotelbook_status_sync = queue_supplier_status_sync


def sync_delay(attempt):
    return timedelta(seconds=min(60 * 2 ** min(attempt, 4), 900))
