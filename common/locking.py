"""Optimistic locking и блокировки строк (ТЗ §3.4)."""
from django.db import models

from common.errors import VersionConflictError


def check_version(instance: models.Model, expected_version) -> None:
    """Сравнивает ожидаемую версию с текущей; 409 VERSION_CONFLICT при расхождении."""
    if expected_version is None:
        raise VersionConflictError(
            current_version=instance.version,
            message="Поле version обязательно для этой операции",
        )
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        raise VersionConflictError(current_version=instance.version) from None
    if expected != instance.version:
        raise VersionConflictError(current_version=instance.version)


def bump_version(instance: models.Model) -> None:
    instance.version = models.F("version") + 1


def locked(model_cls, pk, *, qs=None):
    """Возвращает строку под select_for_update(). Вызывать внутри transaction.atomic()."""
    base = qs if qs is not None else model_cls.objects.all()
    return base.select_for_update().get(pk=pk)
