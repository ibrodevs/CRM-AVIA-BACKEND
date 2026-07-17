from common.models import OutboxEvent
from tenancy.context import get_current_tenant_id


def emit_event(
    event_type: str,
    resource,
    *,
    payload: dict | None = None,
    audience_user=None,
    resource_version: int | None = None,
    tenant_id=None,
) -> OutboxEvent:
    return OutboxEvent.objects.create(
        tenant_id=tenant_id or get_current_tenant_id() or getattr(resource, "tenant_id", None),
        event_type=event_type,
        resource_type=type(resource).__name__ if not isinstance(resource, str) else resource,
        resource_id=str(getattr(resource, "pk", "")) if not isinstance(resource, str) else "",
        resource_version=resource_version
        if resource_version is not None
        else getattr(resource, "version", None),
        payload=payload or {},
        audience_user=audience_user,
    )
