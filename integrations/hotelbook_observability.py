"""HBPRO diagnostics: preserve protocol structure, remove credentials and guest PII."""

import re

from common.logging import redact

_PRIVATE = re.compile(
    r"^(login|first_?name|last_?name|middle_?name|full_?name|name|email|phone|birth_?date|"
    r"dateOfBirth|address|inn|freeRemark|comments|contactInfo|customer|cookie|set-cookie)$",
    re.I,
)
_SECRET = re.compile(r"password|passwd|secret|token|authorization|api.?key", re.I)


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if _PRIVATE.search(str(k)) or _SECRET.search(str(k)) else sanitize(v, secrets)
            for k, v in redact(value).items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        # Provider messages can echo personal details in unstructured text.
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED]", value)
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    return value


def private_values(value):
    result = []
    if isinstance(value, dict):
        for k, v in value.items():
            if _PRIVATE.search(str(k)) or _SECRET.search(str(k)):
                result.extend(scalars(v))
            else:
                result.extend(private_values(v))
    elif isinstance(value, list):
        for v in value:
            result.extend(private_values(v))
    return result


def scalars(value):
    if isinstance(value, dict):
        return [s for v in value.values() for s in scalars(v)]
    if isinstance(value, list):
        return [s for v in value for s in scalars(v)]
    return [value] if isinstance(value, str) and len(value) >= 3 else []


def external_ids(*values):
    ids = {}
    keys = {"searchId", "offerId", "orderId", "providerReferenceId", "itemId", "hotelId", "clientOrderId"}

    def visit(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in keys and isinstance(v, (str, int)):
                    if v not in ids.setdefault(k, []):
                        ids[k].append(v)
                else:
                    visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)

    for value in values:
        visit(value)
    return ids


def normalized_status(items):
    """Confirmation mode is an offer property, never proof of booking confirmation."""
    states = {i.get("status", i.get("state", "UNKNOWN")) for i in items}
    if states == {"CONFIRMED"}:
        return "confirmed"
    if states == {"CANCELED"}:
        return "cancelled"
    if states and states <= {"NEW", "CONFIRM_IN_PROGRESS", "REMARK_IN_PROGRESS", "UNCOMMITTED", "CONFIRMED"}:
        return "pending_supplier_confirmation"
    return "unknown"
