"""Roomlink XML2 contract (API 4.0.1), form POST, no raw XML logging.

https://roomlink.ru/documentation_api, sections 2, 5.5, 5.18-5.22.
XML is untrusted even for authenticated supplier responses.
"""

import http.client
import socket
import time
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from django.conf import settings

from integrations.adapters import AdapterError
from integrations.models import IntegrationLog

SANDBOX_URL = "https://go-api.aanda.ru/"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Multi-rate Availability is 0/1 (sections 5.5/5.18), NOT legacy 2/4.
AANDA_AVAILABILITY_MAP = {"1": "available", "0": "on_request"}
AANDA_STARS_MAP = {"1": 5, "2": 4, "3": 3, "4": 2, "5": 1, "6": 0, "7": 0}
AANDA_ORDER_STATUS_MAP = {
    "11": "pending",
    "18": "pending",
    "20": "pending",
    "21": "pending",
    "25": "failed",
    "35": "booked",
    "47": "pending",
    "65": "booked",
    "71": "cancelled",
}
# Raw descriptions may contain echoed secrets/PII and are deliberately discarded.
AANDA_ERROR_MAP = {
    "1": "AUTH_ERROR",
    "20": "AUTH_ERROR",
    "2": "SEARCH_CRITERIA_INVALID",
    "3": "SEARCH_CRITERIA_INVALID",
    "4": "SEARCH_CRITERIA_INVALID",
    "10": "SEARCH_CRITERIA_INVALID",
    "12": "SEARCH_CRITERIA_INVALID",
    "6": "RATE_UNAVAILABLE",
    "9": "RATE_UNAVAILABLE",
    "49": "RATE_UNAVAILABLE",
    "7": "CANCELLATION_FAILED",
    "8": "BOOKING_NOT_FOUND",
    **{str(code): "INVALID_REQUEST" for code in (5, 11, 13, 14, 15, 16, 17, 18, 19, 21, 50)},
}


def child(parent, tag, **attributes):
    return ET.SubElement(parent, tag, {k: str(v) for k, v in attributes.items()})


def attrs(parent, path):
    node = parent.find(path)
    return dict(node.attrib) if node is not None else {}


def endpoint(environment):
    if environment == "sandbox":
        return SANDBOX_URL
    url = settings.AANDA_PRODUCTION_URL if environment == "production" else ""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AdapterError(
            "PROVIDER_NOT_CONFIGURED", "A&A: HTTPS endpoint production не настроен", category="configuration"
        )
    return url


def exchange(url, body):
    """One POST, no redirects, no transport retries; independent connect/read timeouts."""
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=10)
    try:
        connection.connect()
        connection.sock.settimeout(60)
        connection.request(
            "POST",
            parsed.path or "/",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept": "application/xml, text/xml",
                "User-Agent": "CRM-AVIA-Backend/1.0",
            },
        )
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        return response.status, data
    finally:
        connection.close()


def request(ctx, operation, name, root, config, *, mutation=False):
    url = endpoint(ctx.environment)
    root.set("BuyerId", config["buyer_id"])
    root.set("UserId", config["user_id"])
    root.set("Password", config["password"])
    root.set("Language", config.get("language", "ru").lower())
    body = urlencode({"RequestName": name, "XML": ET.tostring(root, encoding="unicode")}).encode("utf-8")
    started = time.monotonic()
    status, parsed, raw_code, error = None, None, "", None
    try:
        try:
            status, data = exchange(url, body)
        except (TimeoutError, socket.timeout):
            raise AdapterError(
                "TIMEOUT", "A&A: превышено время ожидания", category="timeout", retry_safe=not mutation
            ) from None
        except (OSError, http.client.HTTPException):
            raise AdapterError(
                "SUPPLIER_UNAVAILABLE",
                "A&A: ошибка соединения",
                category="transport",
                retry_safe=not mutation,
            ) from None
        if status >= 500 or status in (408, 429):
            raise AdapterError(
                "SUPPLIER_UNAVAILABLE", "A&A: шлюз недоступен", category="transport", retry_safe=not mutation
            )
        if status in (401, 403):
            try:
                auth_response = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
                code = next(
                    (
                        e.get("Code", "")
                        for e in auth_response.iter()
                        if e.tag in ("Error", "Type") and e.get("Code")
                    ),
                    "",
                )
                raw_code = code if code.isascii() and code.isdigit() and len(code) < 5 else ""
            except (ET.ParseError, DefusedXmlException, ValueError):
                raw_code = ""
            raise AdapterError("AUTH_ERROR", "A&A: доступ отклонён", category="auth")
        if status != 200:
            # Redirects and undocumented HTTP errors cannot prove a mutation failed.
            raise AdapterError(
                "INVALID_RESPONSE",
                "A&A: неожиданный HTTP status",
                category="transport",
                retry_safe=not mutation,
            )
        if not data or len(data) > MAX_RESPONSE_BYTES:
            raise AdapterError(
                "INVALID_RESPONSE",
                "A&A: пустой или слишком большой ответ",
                category="transport",
                retry_safe=not mutation,
            )
        try:
            parsed = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (ET.ParseError, DefusedXmlException, ValueError):
            raise AdapterError(
                "INVALID_RESPONSE", "A&A: некорректный XML", category="transport", retry_safe=not mutation
            ) from None
        errors = parsed.findall(".//Error") + parsed.findall(".//Errors/Type")
        if parsed.tag in ("Error", "Errors"):
            errors.append(parsed)
        if errors:
            item = next((e for e in parsed.iter() if e.tag in ("Type", "Error") and e.get("Code")), errors[0])
            raw_code = item.get("Code", "")
            # Never persist a free-text error code sent by a malicious/upstream echo.
            raw_code = (
                raw_code if raw_code.isascii() and raw_code.isdigit() and len(raw_code) < 5 else "unknown"
            )
            code = AANDA_ERROR_MAP.get(raw_code, "PROVIDER_ERROR")
            raise AdapterError(
                code, f"A&A: {code}", category="auth" if code == "AUTH_ERROR" else "provider", raw=raw_code
            )
        expected = {
            "OrderRequest": "OrderResponse",
            "OrderRequestMulti": "OrderResponse",
            "CountryListRequest": "CountryList",
            "CityListRequest": "CityList",
        }.get(name, name.removesuffix("Request") + "Response")
        if parsed.tag != expected or parsed.find("Success") is None:
            raise AdapterError(
                "INVALID_RESPONSE",
                "A&A: неподтверждённый ответ",
                category="transport",
                retry_safe=not mutation,
            )
        return parsed
    except AdapterError as exc:
        error = exc
        if mutation and exc.category in ("transport", "timeout"):
            error = AdapterError(
                "BOOKING_UNKNOWN" if operation == "order_create" else "CANCELLATION_UNKNOWN",
                "A&A: результат операции неизвестен; выполните status inquiry",
                category="booking_unknown",
                retry_safe=False,
            )
        error.http_status = status
        error.provider_code = raw_code
        raise error from None
    finally:
        # Allowlist summaries instead of redacting raw supplier text. No auth,
        # names, emails, passports, free text or full XML cross this boundary.
        ids = {k: root.get(k) for k in ("Id", "ReferenceNumber", "Hotel", "ProductCode") if root.get(k)}
        for key in ("City", "CountryCode", "Hotels"):
            value = root.get(key, "")
            if value and all(part.isascii() and part.isdigit() and len(part) <= 20 for part in value.split(",")):
                ids[key] = value
        supplier_status = attrs(parsed, "Status").get("Code", "") if parsed is not None else ""
        supplier_status = (
            supplier_status
            if supplier_status.isascii() and supplier_status.isdigit() and len(supplier_status) < 5
            else ""
        )
        if parsed is not None:
            order_id = parsed.get("Id") or attrs(parsed, "Success").get("Id", "")
            if order_id.isascii() and order_id.isdigit() and len(order_id) <= 20:
                ids["OrderId"] = order_id
        IntegrationLog.objects.create(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            order_id=ctx.order_id,
            correlation_id=ctx.correlation_id,
            provider_adapter="aanda",
            environment=ctx.environment,
            operation=operation,
            http_method="POST",
            url=url,
            external_ids=ids,
            request_sanitized={"request_name": name, "external_ids": ids},
            response_sanitized={"success": error is None},
            supplier_status=supplier_status,
            provider_error={"code": raw_code} if raw_code else None,
            http_status=status,
            result="unknown"
            if error and error.category == "booking_unknown"
            else "error"
            if error
            else "success",
            error_code=error.code if error else "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
