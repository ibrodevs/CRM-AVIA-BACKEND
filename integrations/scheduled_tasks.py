"""Queue one read-only HBPRO inquiry per service; the existing cron invokes this."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.jobs import enqueue
from common.models import BackgroundJob
from common.scheduled import scheduled_task
from integrations.hotelbook_observability import normalized_status


def needs_sync(booking):
    return bool(
        booking
        and booking.get("locator")
        and booking.get("state") != "failed"
        and (
            booking.get("state") in ("unknown", "creating_order", "booking", "cancelling")
            or normalized_status(booking.get("items", [])) in ("pending_supplier_confirmation", "unknown")
        )
    )


@scheduled_task("integrations.hotelbook_status_sync")
def queue_hotelbook_status_sync():
    from services.models import OrderService

    queued = 0
    for service_id in (
        OrderService.all_objects.filter(
            archived_at__isnull=True,
            source="api",
            provider_snapshot__provider_adapter="hotelbook",
            provider_snapshot__hotelbook_booking__isnull=False,
        )
        .values_list("id", flat=True)
        .iterator()
    ):
        with transaction.atomic():
            service = OrderService.all_objects.select_for_update().get(pk=service_id)
            booking = (service.provider_snapshot or {}).get("hotelbook_booking", {})
            sync = booking.get("sync", {})
            due = parse_datetime(sync.get("next_at", ""))
            if not needs_sync(booking) or sync.get("exhausted") or (due and due > timezone.now()):
                continue
            if BackgroundJob.objects.filter(
                tenant_id=service.tenant_id,
                kind="integrations.hotelbook_status_sync",
                payload__service_id=str(service.id),
                status__in=["queued", "running"],
            ).exists():
                continue
            enqueue(
                "integrations.hotelbook_status_sync",
                {"service_id": str(service.id)},
                tenant_id=service.tenant_id,
            )
            queued += 1
    return f"queued {queued}"


def sync_delay(attempt):
    return timedelta(seconds=min(60 * 2 ** min(attempt, 4), 900))
