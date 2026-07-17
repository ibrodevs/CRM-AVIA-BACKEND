from dataclasses import dataclass
from typing import Callable

from django.conf import settings

from common.logging import redact
from common.models import BackgroundJob
from tenancy.context import get_current_tenant_id


class JobRetry(Exception):
    """Запросить повтор задания (только для технически безопасных операций)."""

    def __init__(self, message: str = "", delay_seconds: int | None = None):
        super().__init__(message)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class JobHandler:
    kind: str
    func: Callable[[BackgroundJob], dict | None]
    max_attempts: int
    retryable: bool
    user_cancellable: bool


_REGISTRY: dict[str, JobHandler] = {}


def job_handler(
    kind: str, *, max_attempts: int | None = None, retryable: bool = False, user_cancellable: bool = True
):
    def decorator(func):
        _REGISTRY[kind] = JobHandler(
            kind=kind,
            func=func,
            max_attempts=max_attempts or settings.JOB_RUNNER["DEFAULT_MAX_ATTEMPTS"],
            retryable=retryable,
            user_cancellable=user_cancellable,
        )
        return func

    return decorator


def get_handler(kind: str) -> JobHandler | None:
    return _REGISTRY.get(kind)


def enqueue(
    kind: str,
    payload: dict | None = None,
    *,
    priority: int = 100,
    run_after=None,
    initiated_by=None,
    request=None,
    correlation_id: str = "",
    tenant_id=None,
) -> BackgroundJob:
    handler = _REGISTRY.get(kind)
    return BackgroundJob.objects.create(
        tenant_id=tenant_id or get_current_tenant_id(),
        kind=kind,
        payload=redact(payload or {}),
        priority=priority,
        run_after=run_after,
        max_attempts=handler.max_attempts if handler else settings.JOB_RUNNER["DEFAULT_MAX_ATTEMPTS"],
        initiated_by=initiated_by
        or (request.user if request is not None and request.user.is_authenticated else None),
        request_id=getattr(request, "request_id", "") if request is not None else "",
        correlation_id=correlation_id,
    )
