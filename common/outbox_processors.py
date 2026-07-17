import fnmatch
from typing import Callable

from common.models import OutboxEvent

_PROCESSORS: list[tuple[str, Callable[[OutboxEvent], None]]] = []


def outbox_processor(pattern: str):
    def decorator(func):
        _PROCESSORS.append((pattern, func))
        return func

    return decorator


def processors_for(event_type: str) -> list[Callable[[OutboxEvent], None]]:
    return [func for pattern, func in _PROCESSORS if fnmatch.fnmatch(event_type, pattern)]
