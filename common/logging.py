import json
import logging
import re
from datetime import datetime, timezone

_REDACT_KEYS = re.compile(
    r"password|passwd|secret|token|authorization|api_key|apikey|passport|document_number|"
    r"card_number|iban|account_number|cvv",
    re.IGNORECASE,
)


def redact(obj):
    """Рекурсивно маскирует значения чувствительных ключей."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if _REDACT_KEYS.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


class JSONFormatter(logging.Formatter):
    _SKIP = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "travelhub-backend",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(entry), ensure_ascii=False, default=str)
