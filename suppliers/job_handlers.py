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

    queryset = supplier.credentials.filter(tenant_id=supplier.tenant_id, archived_at__isnull=True)
    if (supplier.automation_capabilities or {}).get("adapter") == "ttbooking":
        queryset = queryset.filter(provider_adapter="ttbooking")
        environment = supplier.automation_capabilities.get("ttbooking_environment")
        if environment:
            queryset = queryset.filter(environment=environment)
    credentials = list(queryset)
    if not credentials:
        return {"status": "not_configured", "checked": [], "message": "API-доступ не настроен"}

    results = []
    for credential in credentials:
        result = "ok"
        message = "Адаптер и реквизиты готовы к работе"
        try:
            adapter = get_adapter(credential.provider_adapter)
            if not credential.encrypted_secrets:
                result, message = "no_secrets", "Не заполнены реквизиты доступа"
            elif credential.provider_adapter == "mock" and not settings.ALLOW_MOCK_ADAPTER:
                result, message = "sandbox_disabled", "Sandbox-адаптер запрещён в production"
            elif hasattr(adapter, "verify_credentials"):
                verification_message = adapter.verify_credentials(credential)
                if verification_message:
                    message = str(verification_message)
        except AdapterError as error:
            result, message = (
                ("unknown_adapter" if error.code == "UNKNOWN_ADAPTER" else error.code),
                str(error),
            )

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


def ttbooking_connection_verified(*, tenant_id, supplier_id, environment, status, last_verified_at, rotated_at, created_at):
    """Legacy structural checks are not proof of a working stdCBS connection."""
    if status != "active" or not last_verified_at:
        return False
    from integrations.models import IntegrationLog
    version = (rotated_at or created_at).isoformat()
    return IntegrationLog.objects.filter(tenant_id=tenant_id, supplier_id=supplier_id, provider_adapter="ttbooking",
                                          operation="SEARCHFR", http_status=200, result="success",
                                          request_sanitized__environment=environment,
                                          request_sanitized__credential_version=version).exists()
