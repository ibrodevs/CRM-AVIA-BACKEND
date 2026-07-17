from typing import Callable

_SCHEDULED: dict[str, Callable[[], str | None]] = {}


def scheduled_task(name: str):
    def decorator(func):
        _SCHEDULED[name] = func
        return func

    return decorator


def all_scheduled() -> dict[str, Callable[[], str | None]]:
    return dict(_SCHEDULED)
