"""Tenant-контекст запроса/фонового задания.

Используется TenantManager-ом для автоматической фильтрации queryset-ов и
сервисами для проставления tenant_id при создании записей. В фоновом job
runner контекст устанавливается явно из job.tenant_id.
"""
import contextvars
import uuid
from contextlib import contextmanager

_current_tenant_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)


def get_current_tenant_id() -> uuid.UUID | None:
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: uuid.UUID | None) -> contextvars.Token:
    return _current_tenant_id.set(tenant_id)


@contextmanager
def tenant_context(tenant_id: uuid.UUID | None):
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)
