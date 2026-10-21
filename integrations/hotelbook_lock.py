from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from integrations.adapters import AdapterError
from integrations.models import HotelbookOperationLease


@contextmanager
def booking_lease(service):
    from services.models import OrderService

    owner = uuid4()
    with transaction.atomic():
        OrderService.objects.select_for_update().get(pk=service.id, tenant_id=service.tenant_id)
        lease = HotelbookOperationLease.objects.filter(service=service).first()
        if lease and lease.expires_at > timezone.now():
            raise AdapterError(
                "BOOKING_BUSY", "Сверка поставщика уже выполняется", category="sync", retry_safe=True
            )
        HotelbookOperationLease.objects.update_or_create(
            service=service,
            defaults={
                "owner": owner,
                "expires_at": timezone.now() + timedelta(minutes=10),
            },
        )
    try:
        yield
    finally:
        HotelbookOperationLease.objects.filter(service=service, owner=owner).delete()
