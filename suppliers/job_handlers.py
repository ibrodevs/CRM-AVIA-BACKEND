"""Фоновые задания поставщиков."""
from django.utils import timezone

from common.jobs import job_handler
from common.models import BackgroundJob


@job_handler("suppliers.check_connection", retryable=False)
def check_connection(job: BackgroundJob) -> dict:
    """Проверка соединения с поставщиком через адаптер.

    Пока реальные адаптеры не подключены, используется mock: считает
    соединение успешным при наличии credential (ТЗ §1.1 — adapter stub).
    """
    from suppliers.models import Supplier

    supplier = Supplier.objects.get(pk=job.payload["supplier_id"])
    results = []
    for credential in supplier.credentials.filter(archived_at__isnull=True):
        ok = bool(credential.encrypted_secrets)
        credential.status = "active" if ok else "failed"
        credential.last_verified_at = timezone.now()
        credential.save(update_fields=["status", "last_verified_at"])
        results.append({"credential_id": str(credential.id),
                        "provider_adapter": credential.provider_adapter,
                        "result": "ok" if ok else "no_secrets"})
    return {"checked": results}
