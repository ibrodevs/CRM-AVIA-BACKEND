"""TT Booking stdCBS aviation adapter (phase 1: auth, SEARCHFR, SELECTFR)."""

import hashlib
import itertools
import json
import socket
import time
from datetime import datetime, timezone as dt_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from integrations.adapters import AdapterError, ProviderAdapter
from suppliers.models import SupplierCredential


class TTBookingAdapter(ProviderAdapter):
    key = "ttbooking"
    supported_kinds = ["avia"]
    base_url = "https://www.ttbooking.ru/api/scbs/json"
    http_timeout = 60

    def _credential(self, ctx):
        credential = (
            SupplierCredential.objects.filter(
                tenant_id=ctx.tenant_id,
                supplier_id=ctx.supplier_id,
                supplier__tenant_id=ctx.tenant_id,
                supplier__archived_at__isnull=True,
                provider_adapter=self.key,
                status="active",
                archived_at__isnull=True,
            )
            .select_related("supplier")
            .order_by("id")
            .first()
        )
        if credential is None:
            raise AdapterError("PROVIDER_NOT_CONFIGURED", "Нет активных credentials TT Booking", category="configuration")
        return credential

    @classmethod
    def _parse_config(cls, credential):
        try:
            raw = json.loads(credential.encrypted_secrets)
        except (TypeError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            raise AdapterError("PROVIDER_NOT_CONFIGURED", "Некорректные credentials TT Booking", category="configuration")
        config = {
            **raw,
            "agency": str(raw.get("agency") or "").strip(),
            "user": str(raw.get("user") or raw.get("login") or "").strip(),
            "password": str(raw.get("password") or raw.get("password_key") or "").strip(),
            "url": str(raw.get("url") or cls.base_url).strip(),
            "locale": str(raw.get("locale") or "ru").lower(),
        }
        if not config["agency"] or not config["user"] or not config["password"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: обязательны agency, user/login и password/password_key",
                category="configuration",
            )
        if not config["url"].startswith("https://"):
            raise AdapterError("PROVIDER_NOT_CONFIGURED", "TT Booking: URL API должен использовать HTTPS", category="configuration")
        if config["locale"] != "ru":
            raise AdapterError("PROVIDER_NOT_CONFIGURED", "TT Booking: по документации поддерживается locale=ru", category="configuration")
        currencies = getattr(getattr(credential, "supplier", None), "currencies", None) or []
        config["currency"] = str(raw.get("currency") or (currencies[0] if currencies else "")).upper()
        return config

    def _config(self, ctx):
        credential = self._credential(ctx)
        return self._parse_config(credential)

    def verify_credentials(self, credential):
        self._parse_config(credential)
        return "Реквизиты TT Booking заполнены; live-доступ проверится первым API-запросом"

    @staticmethod
    def build_hash(agency, password, timestamp, user):
        # stdCBS: MD5 от строки GET-параметров agency, password, time, user.
        query = urlencode([
            ("agency", str(agency)),
            ("password", str(password)),
            ("time", str(timestamp)),
            ("user", str(user)),
        ])
        return hashlib.md5(query.encode("utf-8")).hexdigest()

    def _context(self, config, command):
        timestamp = datetime.now(dt_timezone.utc).astimezone().isoformat(timespec="seconds")
        return {
            "agency": config["agency"],
            "user": config["user"],
            "time": timestamp,
            "hash": self.build_hash(config["agency"], config["password"], timestamp, config["user"]),
            "locale": config["locale"],
            "command": command,
        }

    def _wire(self, ctx, config, command, parameters, *, mutation=False):
        payload = {"query": {"context": self._context(config, command), "parameters": parameters}}
        request = Request(
            config["url"],
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        )
        started = time.monotonic()
        status = None
        response = None
        error = None
        try:
            with urlopen(request, timeout=self.http_timeout) as http:
                status = getattr(http, "status", 200)
                response = json.loads(http.read().decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError
        except HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                error = AdapterError("AUTH_ERROR", "TT Booking: ошибка авторизации", category="auth")
            elif mutation and status >= 500:
                error = AdapterError("PROVIDER_RESULT_UNKNOWN", "TT Booking: результат операции неизвестен", category="issue_unknown")
            else:
                error = AdapterError("PROVIDER_ERROR", f"TT Booking: HTTP {status}", category="internal", retry_safe=not mutation and status >= 500)
        except (TimeoutError, socket.timeout, URLError, OSError):
            error = AdapterError(
                "PROVIDER_RESULT_UNKNOWN" if mutation else "TIMEOUT",
                "TT Booking: нет ответа от API",
                category="issue_unknown" if mutation else "timeout",
                retry_safe=not mutation,
            )
        except (ValueError, UnicodeError):
            error = AdapterError("INVALID_RESPONSE", "TT Booking: некорректный JSON", category="internal", retry_safe=not mutation)
        self._log(
            ctx,
            command.lower(),
            {"command": command, "parameter_keys": sorted(parameters)},
            {"received": response is not None},
            result="error" if error else "success",
            error_code=error.code if error else "",
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if error:
            raise error
        return response

    @staticmethod
    def _item(value):
        if isinstance(value, dict):
            return {"code": str(value.get("code") or ""), "name": str(value.get("name") or "")}
        return {"code": str(value or ""), "name": ""}

    @staticmethod
    def _list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _date(value):
        value = str(value or "").strip()
        if not value:
            raise AdapterError("SEARCH_CRITERIA_INVALID", "TT Booking: не указана дата", category="validation")
        return value if "T" in value else f"{value}T00:00:00+00:00"

    def _routes(self, criteria):
        source = criteria.get("route") or criteria.get("segments")
        if isinstance(source, list) and source:
            rows = source
        else:
            origin = criteria.get("origin") or criteria.get("from")
            destination = criteria.get("destination") or criteria.get("to")
            rows = [{"origin": origin, "destination": destination, "date": criteria.get("date") or criteria.get("departure_date")}]
            if criteria.get("return_date") or criteria.get("date_return"):
                rows.append({"origin": destination, "destination": origin, "date": criteria.get("return_date") or criteria.get("date_return")})
        result = []
        for row in rows:
            origin = row.get("origin") or row.get("from") or row.get("locationBegin")
            destination = row.get("destination") or row.get("to") or row.get("locationEnd")
            result.append({
                "date": self._date(row.get("date") or row.get("departure_date")),
                "locationBegin": self._item(origin),
                "locationEnd": self._item(destination),
            })
        if not result or any(not r["locationBegin"]["code"] or not r["locationEnd"]["code"] for r in result):
            raise AdapterError("SEARCH_CRITERIA_INVALID", "TT Booking: укажите аэропорты маршрута", category="validation")
        return result

    @staticmethod
    def _seats(criteria):
        if isinstance(criteria.get("seats"), list) and criteria["seats"]:
            seats = [{"passengerType": str(r.get("passengerType") or r.get("type") or "").upper(), "count": int(r.get("count") or 0)} for r in criteria["seats"]]
        else:
            p = criteria.get("passengers") if isinstance(criteria.get("passengers"), dict) else {}
            seats = [
                {"passengerType": "ADULT", "count": int(p.get("adults", criteria.get("adults", 1)) or 0)},
                {"passengerType": "CHILD", "count": int(p.get("children", criteria.get("children", 0)) or 0)},
                {"passengerType": "INFANT", "count": int(p.get("infants", criteria.get("infants", 0)) or 0)},
            ]
        return [seat for seat in seats if seat["count"] > 0]

    def _search_parameters(self, criteria):
        cabin = str(criteria.get("cabin") or criteria.get("serviceClass") or "ECONOMY").upper()
        cabin = {"PREMIUM_ECONOMY": "PREMIUM", "PREMIUM-ECONOMY": "PREMIUM"}.get(cabin, cabin)
        if cabin not in {"ECONOMY", "BUSINESS", "FIRST", "PREMIUM"}:
            raise AdapterError("SEARCH_CRITERIA_INVALID", f"TT Booking: неизвестный класс {cabin}", category="validation")
        return {
            "route": self._routes(criteria),
            "seats": self._seats(criteria),
            "serviceClass": cabin,
            "skipConnected": bool(criteria.get("direct_only") or criteria.get("skipConnected")) or "",
            "eticketsOnly": criteria.get("etickets_only", criteria.get("eticketsOnly", True)),
            "mixedVendors": criteria.get("mixed_vendors", criteria.get("mixedVendors", True)),
            "preferredAirlines": [self._item(x) for x in (criteria.get("preferred_airlines") or criteria.get("preferredAirlines") or [])],
        }

    def _segment(self, raw):
        carrier = raw.get("carrier") or {}
        baggage = raw.get("baggage") or {}
        return {
            "origin": self._item(raw.get("locationBegin"))["code"],
            "destination": self._item(raw.get("locationEnd"))["code"],
            "departure": raw.get("dateBegin"),
            "arrival": raw.get("dateEnd"),
            "airline": self._item(carrier)["code"],
            "flight_number": str(raw.get("flightNumber") or ""),
            "cabin": str(raw.get("serviceClass") or "").lower(),
            "booking_class": str(raw.get("bookingClass") or ""),
            "fare_basis": str(raw.get("fareBasis") or ""),
            "baggage": baggage.get("type") or baggage.get("value") or "",
            "provider_token": raw.get("token") or "",
        }

    def _normalize_search(self, response, currency):
        respond = response.get("respond") if isinstance(response, dict) else None
        if not isinstance(respond, dict):
            raise AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует respond", category="internal")
        search_token = respond.get("token") or ""
        groups = self._list((respond.get("flightsGroup") or {}).get("flightGroup"))
        offers = []
        for group in groups:
            itineraries = [x for x in self._list((group.get("itineraries") or {}).get("itinerary")) if isinstance(x, dict)]
            choices = [[x for x in self._list((it.get("flights") or {}).get("flight")) if isinstance(x, dict)] for it in itineraries]
            if not choices or any(not x for x in choices):
                continue
            amount = ((group.get("fares") or {}).get("fareTotal") or {}).get("total")
            if amount is None:
                continue
            for selected in itertools.product(*choices):
                segments = [self._segment(seg) for flight in selected for seg in self._list((flight.get("segments") or {}).get("segment")) if isinstance(seg, dict)]
                selected_itineraries = [{"token": it.get("token") or "", "flights": {"flight": [{"token": flight.get("token") or ""}]}} for it, flight in zip(itineraries, selected)]
                selection = {"token": search_token, "flightsGroup": {"flightGroup": [{"token": group.get("token") or "", "itineraries": {"itinerary": selected_itineraries}}]}}
                first = segments[0] if segments else {}
                fares = group.get("fares") or {}
                fare_seats = (fares.get("fareSeats") or {}).get("fareSeat") or (fares.get("fareSeats") or {}).get("seat")
                offers.append({
                    "external_key": "|".join([str(group.get("token") or ""), *[str(f.get("token") or "") for f in selected]]),
                    "provider_adapter": self.key,
                    "kind": "avia",
                    "price": {"amount": str(amount), "currency": currency},
                    "availability": "available",
                    "expires_at": group.get("timeLimit"),
                    "itinerary": {"segments": segments},
                    "fare": {
                        "cabin": first.get("cabin", ""),
                        "booking_class": first.get("booking_class", ""),
                        "fare_basis": first.get("fare_basis", ""),
                        "eticket": bool(group.get("eticket")),
                        "gds": group.get("gds") or "",
                        "validating_carrier": self._item(group.get("carrier"))["code"],
                        "passenger_prices": self._list(fare_seats),
                    },
                    "provider_selection": selection,
                    "provider_search_token": search_token,
                })
        return offers

    def search(self, ctx, kind, criteria):
        if kind != "avia":
            raise AdapterError("UNSUPPORTED_SERVICE", "TT Booking: поддерживается только авиа", category="configuration")
        config = self._config(ctx)
        if not config["currency"]:
            raise AdapterError("PROVIDER_NOT_CONFIGURED", "TT Booking: у поставщика не указана валюта", category="configuration")
        return self._normalize_search(self._wire(ctx, config, "SEARCHFR", self._search_parameters(criteria)), config["currency"])

    def revalidate(self, ctx, offer_snapshot):
        selection = offer_snapshot.get("provider_selection")
        if not isinstance(selection, dict):
            raise AdapterError("OFFER_NOT_REVALIDATABLE", "TT Booking: отсутствуют токены выбора", category="validation")
        config = self._config(ctx)
        offers = self._normalize_search(self._wire(ctx, config, "SELECTFR", selection), config["currency"])
        if not offers:
            raise AdapterError("RATE_UNAVAILABLE", "TT Booking: тариф недоступен", category="availability")
        return {"status": "valid", "price": offers[0]["price"], "snapshot": offers[0]}

    def fare_rules(self, ctx, offer_snapshot):
        raise AdapterError("NOT_IMPLEMENTED", "TT Booking: тарифные правила — следующий этап", category="configuration")

    def book(self, ctx, booking_request):
        raise AdapterError("NOT_IMPLEMENTED", "TT Booking: CREATEBR — следующий этап", category="configuration")

    def retrieve_booking(self, ctx, locator):
        return self._wire(ctx, self._config(ctx), "DISPLAYSTATUS", {"token": locator})

    def issue(self, ctx, issue_request):
        token = issue_request.get("locator") or issue_request.get("token")
        if not token:
            raise AdapterError("VALIDATION_ERROR", "TT Booking: не указан token бронирования", category="validation")
        return self._wire(ctx, self._config(ctx), "PAYDEPOSIT", {"token": token}, mutation=True)

    def cancel(self, ctx, locator):
        return self._wire(ctx, self._config(ctx), "CANCELBOOKING", {"token": locator}, mutation=True)

    def refund_quote(self, ctx, request):
        raise AdapterError("NOT_IMPLEMENTED", "TT Booking: расчёт возврата отсутствует в stdCBS", category="configuration")
