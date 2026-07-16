"""Реестр provider adapters (ТЗ §1.1, §13).

Каждый внешний поставщик закрывается адаптером с единым интерфейсом.
Пока реальный провайдер не выбран, используется MockAdapter с тем же
интерфейсом, идемпотентностью, логами запросов и моделированием ошибок.
Provider-specific JSON не попадает в основные бизнес-модели — только в
provider_snapshot/raw_snapshot.
"""
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from integrations.models import IntegrationLog


class AdapterError(Exception):
    """Нормализованная ошибка адаптера (категории — ТЗ §13)."""

    def __init__(self, code: str, message: str = "", *, category: str = "internal",
                 retry_safe: bool = False, raw: str = ""):
        super().__init__(message or code)
        self.code = code
        self.category = category  # auth/timeout/availability/passenger/issue_unknown/payment/sync/internal
        self.retry_safe = retry_safe
        self.raw = raw


class AmbiguousResultError(AdapterError):
    """Timeout с неизвестным результатом: повтор запрещён до status inquiry (ТЗ §9.1)."""

    def __init__(self, message: str = "Результат операции неизвестен"):
        super().__init__("ISSUE_UNKNOWN", message, category="issue_unknown", retry_safe=False)


@dataclass
class AdapterContext:
    tenant_id: object
    supplier_id: object | None = None
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class ProviderAdapter:
    """Базовый интерфейс адаптера. Методы поднимают AdapterError."""

    key = "base"
    supported_kinds: list[str] = []

    def search(self, ctx: AdapterContext, kind: str, criteria: dict) -> list[dict]:
        raise NotImplementedError

    def revalidate(self, ctx: AdapterContext, offer_snapshot: dict) -> dict:
        raise NotImplementedError

    def fare_rules(self, ctx: AdapterContext, offer_snapshot: dict) -> dict:
        raise NotImplementedError

    def book(self, ctx: AdapterContext, booking_request: dict) -> dict:
        raise NotImplementedError

    def retrieve_booking(self, ctx: AdapterContext, locator: str) -> dict:
        raise NotImplementedError

    def issue(self, ctx: AdapterContext, issue_request: dict) -> dict:
        raise NotImplementedError

    def cancel(self, ctx: AdapterContext, locator: str) -> dict:
        raise NotImplementedError

    def refund_quote(self, ctx: AdapterContext, request: dict) -> dict:
        raise NotImplementedError

    # --- журналирование ---------------------------------------------------

    def _log(self, ctx: AdapterContext, operation: str, request: dict, response: dict | None,
             *, result: str, error_code: str = "", duration_ms: int = 0,
             http_status: int | None = 200) -> IntegrationLog:
        return IntegrationLog.objects.create(
            tenant_id=ctx.tenant_id,
            correlation_id=ctx.correlation_id,
            supplier_id=ctx.supplier_id,
            provider_adapter=self.key,
            operation=operation,
            request_sanitized=_sanitize(request),
            response_sanitized=_sanitize(response) if response is not None else None,
            http_status=http_status,
            result=result,
            error_code=error_code,
            duration_ms=duration_ms,
        )


def _sanitize(payload: dict | None) -> dict | None:
    from common.logging import redact

    return redact(payload) if payload is not None else None


class MockAdapter(ProviderAdapter):
    """Sandbox-адаптер: детерминированные офферы, задержки и моделирование ошибок.

    Управление поведением через criteria["_mock"]:
      {"fail": "timeout"|"auth"|"availability", "offers": N, "delay_ms": 100}
    """

    key = "mock"
    supported_kinds = ["avia", "rail", "hotel", "transfer", "bus", "tour",
                       "aeroexpress", "lounge", "insurance", "visa", "other"]

    _bookings: dict[str, dict] = {}  # in-memory состояние sandbox

    def search(self, ctx: AdapterContext, kind: str, criteria: dict) -> list[dict]:
        started = time.monotonic()
        mock = criteria.get("_mock", {})
        if mock.get("delay_ms"):
            time.sleep(mock["delay_ms"] / 1000)
        if fail := mock.get("fail"):
            self._log(ctx, "search", criteria, None, result="error", error_code=fail.upper(),
                      http_status=None)
            raise AdapterError(fail.upper(), f"Mock failure: {fail}",
                               category=fail, retry_safe=fail == "timeout")
        count = int(mock.get("offers", 3))
        offers = [self._make_offer(kind, criteria, index) for index in range(count)]
        self._log(ctx, "search", criteria, {"offers_count": len(offers)},
                  result="success", duration_ms=int((time.monotonic() - started) * 1000))
        return offers

    def _make_offer(self, kind: str, criteria: dict, index: int) -> dict:
        seed = hashlib.sha256(
            json.dumps({"kind": kind, "criteria": {k: v for k, v in criteria.items()
                                                   if k != "_mock"}, "i": index},
                       sort_keys=True, default=str).encode()
        ).hexdigest()
        base_price = Decimal(150 + (int(seed[:6], 16) % 600) + index * 40)
        departure = datetime.fromisoformat(str(criteria.get("date", "2026-08-01"))) \
            if criteria.get("date") else datetime(2026, 8, 1)
        offer = {
            "external_key": f"MOCK-{seed[:12].upper()}",
            "kind": kind,
            "price": {"amount": str(base_price), "currency": criteria.get("currency", "USD")},
            "availability": "available",
            "expires_at": (datetime.now().astimezone() + timedelta(minutes=30)).isoformat(),
        }
        if kind == "avia":
            offer["itinerary"] = {
                "segments": [{
                    "origin": criteria.get("origin", "FRU"),
                    "destination": criteria.get("destination", "IST"),
                    "departure": (departure + timedelta(hours=8 + index * 3)).isoformat(),
                    "arrival": (departure + timedelta(hours=13 + index * 3)).isoformat(),
                    "airline": ["TK", "PC", "J2"][index % 3],
                    "flight_number": f"{100 + index}",
                    "cabin": criteria.get("cabin", "economy"),
                }],
            }
            offer["fare"] = {
                "cabin": criteria.get("cabin", "economy"),
                "booking_class": "Y",
                "baggage": "1PC" if index % 2 == 0 else "0PC",
                "refundable": index % 2 == 0,
            }
        elif kind == "hotel":
            offer["itinerary"] = {
                "property_name": f"Mock Hotel {index + 1}",
                "city": criteria.get("location", "Bishkek"),
                "stars": 3 + index % 3,
                "check_in": criteria.get("check_in"),
                "check_out": criteria.get("check_out"),
                "room": "Standard Double",
                "meal_plan": ["RO", "BB", "HB"][index % 3],
            }
        else:
            offer["itinerary"] = {"description": f"Mock {kind} option {index + 1}",
                                  **{k: v for k, v in criteria.items() if k != "_mock"}}
        return offer

    def revalidate(self, ctx: AdapterContext, offer_snapshot: dict) -> dict:
        self._log(ctx, "revalidate", {"external_key": offer_snapshot.get("external_key")},
                  {"status": "valid"}, result="success")
        return {"status": "valid", "price": offer_snapshot.get("price")}

    def fare_rules(self, ctx: AdapterContext, offer_snapshot: dict) -> dict:
        rules = {"refund": "Возврат до вылета со штрафом 25%",
                 "exchange": "Обмен разрешён со сбором 30 USD",
                 "no_show": "No-show: возврат запрещён"}
        self._log(ctx, "fare_rules", {"external_key": offer_snapshot.get("external_key")},
                  rules, result="success")
        return rules

    def book(self, ctx: AdapterContext, booking_request: dict) -> dict:
        client_ref = booking_request.get("client_request_id", "")
        if client_ref and client_ref in self._bookings:
            return self._bookings[client_ref]  # идемпотентность по client request id
        if booking_request.get("_mock", {}).get("fail") == "availability":
            self._log(ctx, "book", booking_request, None, result="error",
                      error_code="AVAILABILITY_CONFLICT")
            raise AdapterError("AVAILABILITY_CONFLICT", "Мест больше нет",
                               category="availability")
        locator = f"MK{uuid.uuid4().hex[:6].upper()}"
        result = {
            "locator": locator,
            "status": "booked",
            "ticketing_deadline": (datetime.now().astimezone() + timedelta(hours=24)).isoformat(),
        }
        if client_ref:
            self._bookings[client_ref] = result
        self._bookings[locator] = result
        self._log(ctx, "book", booking_request, result, result="success")
        return result

    def retrieve_booking(self, ctx: AdapterContext, locator: str) -> dict:
        booking = self._bookings.get(locator)
        if booking is None:
            self._log(ctx, "retrieve", {"locator": locator}, None, result="error",
                      error_code="NOT_FOUND", http_status=404)
            raise AdapterError("BOOKING_NOT_FOUND", category="sync")
        self._log(ctx, "retrieve", {"locator": locator}, booking, result="success")
        return booking

    def issue(self, ctx: AdapterContext, issue_request: dict) -> dict:
        if issue_request.get("_mock", {}).get("fail") == "timeout":
            self._log(ctx, "issue", issue_request, None, result="unknown",
                      error_code="ISSUE_UNKNOWN", http_status=None)
            raise AmbiguousResultError()
        locator = issue_request.get("locator", "")
        tickets = [
            {"passenger_ref": p, "ticket_number": f"999-{uuid.uuid4().hex[:10]}"}
            for p in (issue_request.get("passengers") or ["P1"])
        ]
        result = {"locator": locator, "status": "issued", "tickets": tickets}
        if locator in self._bookings:
            self._bookings[locator] = {**self._bookings[locator], **result}
        self._log(ctx, "issue", issue_request, result, result="success")
        return result

    def cancel(self, ctx: AdapterContext, locator: str) -> dict:
        result = {"locator": locator, "status": "cancelled"}
        if locator in self._bookings:
            self._bookings[locator] = {**self._bookings[locator], **result}
        self._log(ctx, "cancel", {"locator": locator}, result, result="success")
        return result

    def refund_quote(self, ctx: AdapterContext, request: dict) -> dict:
        paid = Decimal(str(request.get("paid_amount", "0")))
        penalty = (paid * Decimal("0.25")).quantize(Decimal("0.01"))
        result = {"penalty": str(penalty), "currency": request.get("currency", "USD"),
                  "refundable": str(paid - penalty)}
        self._log(ctx, "refund_quote", request, result, result="success")
        return result


_ADAPTERS: dict[str, ProviderAdapter] = {}


def register_adapter(adapter: ProviderAdapter) -> None:
    _ADAPTERS[adapter.key] = adapter


def get_adapter(key: str) -> ProviderAdapter:
    adapter = _ADAPTERS.get(key)
    if adapter is None:
        raise AdapterError("UNKNOWN_ADAPTER", f"Адаптер '{key}' не зарегистрирован")
    return adapter


register_adapter(MockAdapter())
