"""Проверка прав RBAC (ТЗ §5.3).

user_permission_codes() кэшируется на объекте пользователя в рамках запроса.
DRF-классы: require("orders.view") для view-уровня; object-level правила
реализуются в queryset-ах приложений (например, оператор видит только свои
заказы) и дополнительных проверках сервисов.
"""
from rest_framework.permissions import BasePermission

from accounts.models import RolePermission


def user_permission_codes(user) -> frozenset[str]:
    cached = getattr(user, "_perm_codes", None)
    if cached is not None:
        return cached
    if getattr(user, "is_superuser", False):
        from accounts.permissions_catalog import PERMISSIONS

        codes = frozenset(PERMISSIONS.keys())
    else:
        codes = frozenset(
            RolePermission.objects.filter(role__user_roles__user=user)
            .values_list("permission_code", flat=True)
        )
    user._perm_codes = codes
    return codes


def has_permission(user, code: str) -> bool:
    return code in user_permission_codes(user)


def has_service_action(user, service_kind: str, action: str) -> bool:
    """Область прав по типу услуги. Пустой список записей = ограничений нет."""
    access = getattr(user, "_service_access", None)
    if access is None:
        access = {sa.service_kind: set(sa.allowed_actions) for sa in user.service_access.all()}
        user._service_access = access
    if not access:
        return True
    return action in access.get(service_kind, set())


def require(*codes: str):
    """DRF permission: у пользователя должен быть хотя бы один из кодов."""

    class _RequirePermission(BasePermission):
        message = f"Требуется право: {' или '.join(codes)}"

        def has_permission(self, request, view):  # noqa: ARG002
            user = request.user
            if not user or not user.is_authenticated:
                return False
            granted = user_permission_codes(user)
            return any(code in granted for code in codes)

    return _RequirePermission
