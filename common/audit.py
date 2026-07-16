"""Запись аудита. Append-only, с redaction чувствительных полей."""
from common.logging import redact
from common.models import AuditEvent
from tenancy.context import get_current_tenant_id


def audit(
    action: str,
    *,
    actor=None,
    resource=None,
    resource_type: str = "",
    resource_id: str = "",
    request=None,
    reason: str = "",
    before: dict | None = None,
    after: dict | None = None,
    tenant_id=None,
) -> AuditEvent:
    if resource is not None:
        resource_type = resource_type or type(resource).__name__
        resource_id = resource_id or str(resource.pk)
    ip = user_agent = ""
    request_id = ""
    if request is not None:
        request_id = getattr(request, "request_id", "") or ""
        ip = _client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:512]
        if actor is None and getattr(request, "user", None) is not None and request.user.is_authenticated:
            actor = request.user
    return AuditEvent.objects.create(
        tenant_id=tenant_id or get_current_tenant_id(),
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        request_id=request_id,
        ip_address=ip or None,
        user_agent=user_agent,
        reason=reason,
        before=redact(before) if before else None,
        after=redact(after) if after else None,
    )


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
