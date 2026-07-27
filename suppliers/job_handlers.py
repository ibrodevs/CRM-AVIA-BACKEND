from django.conf import settings
from django.utils import timezone

from common.jobs import job_handler
from common.models import BackgroundJob


def verify_supplier_credentials(supplier) -> dict:
    """Validate that credentials can be routed to an installed adapter.

    The sandbox adapter is an explicit, working integration mode in
    development. Production never reports it as connected when mocks are
    disabled. Unknown adapters and empty secrets are returned as failures
    instead of a misleading successful queued job.
    """
    from integrations.adapters import AdapterError, get_adapter

    credentials = list(supplier.credentials.filter(archived_at__isnull=True))
    if not credentials:
        return {"status": "not_configured", "checked": [], "message": "API-доступ не настроен"}

    results = []
    for credential in credentials:
        result = "ok"
        message = "Адаптер и реквизиты готовы к работе"
        try:
            get_adapter(credential.provider_adapter)
            if not credential.encrypted_secrets:
                result, message = "no_secrets", "Не заполнены реквизиты доступа"
            elif credential.provider_adapter == "mock" and not settings.ALLOW_MOCK_ADAPTER:
                result, message = "sandbox_disabled", "Sandbox-адаптер запрещён в production"
        except AdapterError as error:
            result, message = "unknown_adapter", str(error)

        credential.status = "active" if result == "ok" else "failed"
        credential.last_verified_at = timezone.now()
        credential.save(update_fields=["status", "last_verified_at"])
        results.append(
            {
                "credential_id": str(credential.id),
                "provider_adapter": credential.provider_adapter,
                "environment": credential.environment,
                "result": result,
                "message": message,
                "verified_at": credential.last_verified_at.isoformat(),
            }
        )

    connected = any(item["result"] == "ok" for item in results)
    return {
        "status": "connected" if connected else "failed",
        "checked": results,
        "message": "Подключение готово" if connected else "Подключение требует настройки",
    }


@job_handler("suppliers.check_connection", retryable=False)
def check_connection(job: BackgroundJob) -> dict:
    """Background-compatible wrapper for scheduled credential checks."""
    from suppliers.models import Supplier

    supplier = Supplier.objects.get(pk=job.payload["supplier_id"])
    return verify_supplier_credentials(supplier)
