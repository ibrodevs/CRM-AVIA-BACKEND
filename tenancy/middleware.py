"""Устанавливает tenant-контекст из аутентифицированного пользователя.

DRF аутентифицирует пользователя позже middleware, поэтому для API-запросов
tenant дополнительно проставляется в accounts.authentication после проверки
JWT. Здесь покрывается session-auth (Django admin) и сброс контекста.
"""
from .context import set_current_tenant_id


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            tenant_id = getattr(user, "tenant_id", None)
        token = set_current_tenant_id(tenant_id)
        try:
            return self.get_response(request)
        finally:
            from .context import _current_tenant_id

            _current_tenant_id.reset(token)
