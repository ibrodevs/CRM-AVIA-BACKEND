"""Scoped read-only integration credentials issued from Settings."""
import hashlib
import secrets

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from accounts.permissions import user_permission_codes
from common.models import WorkspaceAction
from tenancy.context import set_current_tenant_id


class IntegrationKeyAuthentication(BaseAuthentication):
    def authenticate_header(self, request):
        return "ApiKey" if request.headers.get("X-API-Key") else "Bearer"

    def authenticate(self, request):
        token = request.headers.get("X-API-Token")
        key = request.headers.get("X-API-Key")
        if not token and not key:
            return None
        if not token or not key:
            raise AuthenticationFailed("Нужны X-API-Token и X-API-Key")
        record = WorkspaceAction.objects.select_related("created_by").filter(
            action="integration.api_key.generate", status="completed",
            result__token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        ).first()
        if not record or not secrets.compare_digest(record.result.get("key_sha256", ""), hashlib.sha256(key.encode()).hexdigest()):
            raise AuthenticationFailed("Ключ недействителен или отозван")
        user = record.created_by
        if not user or not user.is_active or user.tenant_id != record.tenant_id:
            raise AuthenticationFailed("Владелец ключа неактивен")
        access = record.payload.get("access", [])
        allowed = []
        if "Получение аналитики" in access:
            allowed += ["dashboard/", "reports/"]
        if "Получение информации о клиентах" in access:
            allowed += ["clients/", "persons/", "companies/"]
        if "Доступ к данным" in access:
            allowed += ["orders/", "services/", "meta/"]
        path = request.path.removeprefix("/api/v1/")
        if request.method not in ("GET", "HEAD", "OPTIONS") or not any(path.startswith(prefix) for prefix in allowed):
            raise PermissionDenied("Ключ разрешает только выбранные разделы API для чтения")
        # Restrict permissions in addition to endpoint allowlist; passport details remain unavailable.
        user._perm_codes = frozenset(code for code in user_permission_codes(user) if code.endswith(".view"))
        set_current_tenant_id(user.tenant_id)
        return user, None
