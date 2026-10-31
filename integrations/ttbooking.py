"""TTBooking stdCBS adapter with explicit legacy search compatibility."""

import hashlib
import itertools
import json
import socket
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener

from integrations.adapters import AdapterError
from integrations.json_provider import JsonProviderAdapter, NoSupplierRedirect
from integrations.ttbooking_async import TTBookingAsyncMixin
from suppliers.models import SupplierCredential


class TTBookingAdapter(TTBookingAsyncMixin, JsonProviderAdapter):
    environment_setting = "TTBOOKING_ENVIRONMENT"
    requires_booking_contact = True
    required_booking_contact_fields = ("email", "phone")
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
                environment=self.configured_environment(),
                status="active",
                archived_at__isnull=True,
            )
            .select_related("supplier")
            .order_by("id")
            .first()
        )
        if credential is None:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Нет активных credentials TT Booking", category="configuration"
            )
        return credential

    @classmethod
    def _parse_config(cls, credential):
        try:
            raw = json.loads(credential.encrypted_secrets)
        except (TypeError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Некорректные credentials TT Booking", category="configuration"
            )
        config = {
            **raw,
            "agency": str(raw.get("agency") or "").strip(),
            "user": str(raw.get("user") or raw.get("login") or "").strip(),
            "password": str(raw.get("password") or raw.get("password_key") or "").strip(),
            "url": str(
                raw.get("url")
                or (
                    "https://dev.ttbooking.ru/api/scbs/json"
                    if credential.environment == "sandbox"
                    else cls.base_url
                )
            ).strip(),
            "locale": str(raw.get("locale") or "ru").lower(),
            "protocol": raw.get("protocol", "async"),
        }
        if not config["agency"] or not config["user"] or not config["password"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: обязательны agency, user/login и password/password_key",
                category="configuration",
            )
        parsed = urlparse(config["url"])
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: URL API должен использовать HTTPS",
                category="configuration",
            )
        if config["protocol"] not in ("async", "legacy"):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Неизвестный протокол TTBooking", category="configuration"
            )
        for key in ("status_map", "ticket_fields"):
            if key in config and not isinstance(config[key], dict):
                raise AdapterError(
                    "PROVIDER_NOT_CONFIGURED", "Нужен JSON-объект " + key, category="configuration"
                )
        if config["locale"] != "ru":
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: по документации поддерживается locale=ru",
                category="configuration",
            )
        currencies = getattr(getattr(credential, "supplier", None), "currencies", None) or []
        config["currency"] = str(raw.get("currency") or (currencies[0] if currencies else "")).upper()
        return config

    def _config(self, ctx):
        credential = self._credential(ctx)
        return self._parse_config(credential)

    def verify_credentials(self, credential):
        self._parse_config(credential)
        return {
            "verified": False,
            "message": "Реквизиты TTBooking настроены; доступ подтвердится успешным поиском",
        }

    @staticmethod
    def build_hash(agency, password, timestamp, user):
        # stdCBS: MD5 от строки GET-параметров agency, password, time, user.
        query = urlencode(
            [
                ("agency", str(agency)),
                ("password", str(password)),
                ("time", str(timestamp)),
                ("user", str(user)),
            ]
        )
        return hashlib.md5(query.encode("utf-8")).hexdigest()

    def _context(self, config, command):
        timestamp = datetime.now(dt_timezone.utc).astimezone().isoformat(timespec="seconds")
        return {
            "agency": config["agency"],
            "user": config["user"],
            "time": timestamp,
            "hash": (
                hashlib.md5(
                    f"agency={config['agency']}&password={config['password']}&time={timestamp}&user={config['user']}".encode()
                ).hexdigest()
                if config["protocol"] == "async"
                else self.build_hash(config["agency"], config["password"], timestamp, config["user"])
            ),
            "locale": config["locale"],
            "command": command,
        }

    def _wire(self, ctx, config, command, parameters, *, mutation=False):
        payload = {"context": self._context(config, command), "parameters": parameters}
        if config["protocol"] == "legacy":
            payload = {"query": payload}
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
            with build_opener(NoSupplierRedirect()).open(request, timeout=self.http_timeout) as http:
                status = getattr(http, "status", 200)
                raw = http.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ValueError
                response = json.loads(raw.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError
        except HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                error = AdapterError("AUTH_ERROR", "TT Booking: ошибка авторизации", category="auth")
            elif mutation and (status >= 500 or 300 <= status < 400):
                error = AdapterError(
                    "PROVIDER_RESULT_UNKNOWN",
                    "TT Booking: результат операции неизвестен",
                    category="booking_unknown",
                )
            else:
                error = AdapterError(
                    "PROVIDER_ERROR",
                    f"TT Booking: HTTP {status}",
                    category="internal",
                    retry_safe=not mutation and status >= 500,
                )
        except (TimeoutError, socket.timeout, URLError, OSError):
            error = AdapterError(
                "PROVIDER_RESULT_UNKNOWN" if mutation else "TIMEOUT",
                "TT Booking: нет ответа от API",
                category="booking_unknown" if mutation else "timeout",
                retry_safe=not mutation,
            )
        except (ValueError, UnicodeError):
            error = AdapterError(
                "INVALID_RESPONSE",
                "TT Booking: некорректный JSON",
                category="booking_unknown" if mutation else "internal",
                retry_safe=not mutation,
            )
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
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "TT Booking: не указана дата", category="validation"
            )
        return value if "T" in value else f"{value}T00:00:00+00:00"

    def _routes(self, criteria):
        source = criteria.get("route") or criteria.get("segments")
        if isinstance(source, list) and source:
            rows = source
        else:
            origin = criteria.get("origin") or criteria.get("from")
            destination = criteria.get("destination") or criteria.get("to")
            rows = [
                {
                    "origin": origin,
                    "destination": destination,
                    "date": criteria.get("date") or criteria.get("departure_date"),
                }
            ]
            if criteria.get("return_date") or criteria.get("date_return"):
                rows.append(
                    {
                        "origin": destination,
                        "destination": origin,
                        "date": criteria.get("return_date") or criteria.get("date_return"),
                    }
                )
        result = []
        for row in rows:
            origin = row.get("origin") or row.get("from") or row.get("locationBegin")
            destination = row.get("destination") or row.get("to") or row.get("locationEnd")
            result.append(
                {
                    "date": self._date(row.get("date") or row.get("departure_date")),
                    "locationBegin": self._item(origin),
                    "locationEnd": self._item(destination),
                }
            )
        if not result or any(not r["locationBegin"]["code"] or not r["locationEnd"]["code"] for r in result):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "TT Booking: укажите аэропорты маршрута", category="validation"
            )
        return result

    @staticmethod
    def _seats(criteria):
        if isinstance(criteria.get("seats"), list) and criteria["seats"]:
            seats = [
                {
                    "passengerType": str(r.get("passengerType") or r.get("type") or "").upper(),
                    "count": int(r.get("count") or 0),
                }
                for r in criteria["seats"]
            ]
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
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", f"TT Booking: неизвестный класс {cabin}", category="validation"
            )
        return {
            "route": self._routes(criteria),
            "seats": self._seats(criteria),
            "serviceClass": cabin,
            "skipConnected": bool(criteria.get("direct_only") or criteria.get("skipConnected")) or "",
            "eticketsOnly": criteria.get("etickets_only", criteria.get("eticketsOnly", True)),
            "mixedVendors": criteria.get("mixed_vendors", criteria.get("mixedVendors", True)),
            "preferredAirlines": [
                self._item(x)
                for x in (criteria.get("preferred_airlines") or criteria.get("preferredAirlines") or [])
            ],
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
            "provider_token": raw.get("token", ""),
        }

    @staticmethod
    def _currency(value, fallback):
        if value in (None, ""):
            return fallback
        numeric = {
            "643": "RUB",
            "810": "RUB",
            "840": "USD",
            "978": "EUR",
            "417": "KGS",
            "398": "KZT",
            "156": "CNY",
            "826": "GBP",
            "392": "JPY",
            "784": "AED",
            "756": "CHF",
        }
        code = numeric.get(str(value), str(value).upper())
        if len(code) != 3 or not code.isalpha():
            raise AdapterError(
                "CURRENCY_UNSUPPORTED", "TTBooking: неподдерживаемый код валюты", category="configuration"
            )
        return code

    def _normalize_search(self, response, currency):
        respond = response.get("respond") if isinstance(response, dict) else None
        if not isinstance(respond, dict):
            raise AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует respond", category="internal")
        search_token = respond.get("token", "")
        groups = self._list((respond.get("flightsGroup") or {}).get("flightGroup"))
        offers = []
        for group in groups:
            itineraries = [
                x
                for x in self._list((group.get("itineraries") or {}).get("itinerary"))
                if isinstance(x, dict)
            ]
            choices = [
                [x for x in self._list((it.get("flights") or {}).get("flight")) if isinstance(x, dict)]
                for it in itineraries
            ]
            if not choices or any(not x for x in choices):
                continue
            amount = ((group.get("fares") or {}).get("fareTotal") or {}).get("total")
            if amount is None:
                continue
            for selected in itertools.product(*choices):
                segments = [
                    self._segment(seg)
                    for flight in selected
                    for seg in self._list((flight.get("segments") or {}).get("segment"))
                    if isinstance(seg, dict)
                ]
                selected_itineraries = [
                    {
                        "token": it.get("token", ""),
                        "flights": {"flight": [{"token": flight.get("token", "")}]},
                    }
                    for it, flight in zip(itineraries, selected, strict=True)
                ]
                selection = {
                    "token": search_token,
                    "flightsGroup": {
                        "flightGroup": [
                            {
                                "token": group.get("token", ""),
                                "itineraries": {"itinerary": selected_itineraries},
                            }
                        ]
                    },
                }
                first = segments[0] if segments else {}
                fares = group.get("fares") or {}
                fare_seats = (fares.get("fareSeats") or {}).get("fareSeat") or (
                    fares.get("fareSeats") or {}
                ).get("seat")
                offers.append(
                    {
                        "external_key": "|".join(
                            [str(group.get("token", "")), *[str(f.get("token", "")) for f in selected]]
                        ),
                        "provider_adapter": self.key,
                        "kind": "avia",
                        "price": {
                            "amount": str(amount),
                            "currency": self._currency(fares.get("fareCurrency"), currency),
                        },
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
                            "passenger_prices": [
                                {**r, "count": int(r.get("count") or 0)} for r in self._list(fare_seats)
                            ],
                        },
                        "provider_selection": selection,
                        "provider_search_token": search_token,
                    }
                )
        return offers

    def search(self, ctx, kind, criteria):
        if kind != "avia":
            raise AdapterError(
                "UNSUPPORTED_SERVICE", "TT Booking: поддерживается только авиа", category="configuration"
            )
        config = self._config(ctx)
        if not config["currency"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: у поставщика не указана валюта",
                category="configuration",
            )
        return (
            self._async_search(ctx, config, criteria)
            if config["protocol"] == "async"
            else self._normalize_search(
                self._wire(ctx, config, "SEARCHFR", self._search_parameters(criteria)), config["currency"]
            )
        )

    def revalidate(self, ctx, offer_snapshot):
        config = self._config(ctx)
        if config["protocol"] == "async":
            return self._async_revalidate(ctx, config, offer_snapshot)
        selection = offer_snapshot.get("provider_selection")
        if not isinstance(selection, dict):
            raise AdapterError(
                "OFFER_NOT_REVALIDATABLE", "TT Booking: отсутствуют токены выбора", category="validation"
            )
        config = self._config(ctx)
        offers = self._normalize_search(self._wire(ctx, config, "SELECTFR", selection), config["currency"])
        if not offers:
            raise AdapterError("RATE_UNAVAILABLE", "TT Booking: тариф недоступен", category="availability")
        return {"status": "valid", "price": offers[0]["price"], "snapshot": offers[0]}

    def fare_rules(self, ctx, offer_snapshot):
        config = self._config(ctx)
        if config["protocol"] == "async":
            token = (offer_snapshot.get("ttbooking") or {}).get("selection_token")
            if not token:
                raise AdapterError("PREFLIGHT_REQUIRED", "Сначала перепроверьте тариф", category="validation")
            return self._wire(ctx, config, "SELECT_UPT", {"token": token})
        raise AdapterError(
            "NOT_SUPPORTED", "Для правил тарифа нужен async-протокол", category="configuration"
        )

    def book(self, ctx, booking_request):
        if self._config(ctx)["protocol"] != "async":
            raise AdapterError(
                "PROTOCOL_UPGRADE_REQUIRED",
                "Бронирование поддерживается через async-протокол из консоли TTBooking",
                category="configuration",
            )
        return self._async_book(ctx, booking_request)

    def retrieve_booking(self, ctx, locator):
        if self._config(ctx)["protocol"] == "async":
            return self._async_status(ctx, locator)
        return self._wire(ctx, self._config(ctx), "DISPLAYSTATUS", {"token": locator})

    def issue(self, ctx, issue_request):
        if self._config(ctx)["protocol"] != "async":
            raise AdapterError(
                "PROTOCOL_UPGRADE_REQUIRED",
                "Для выписки нужен split-протокол stdCBS",
                category="configuration",
            )
        return self._async_mutation(ctx, issue_request["locator"], "PAYDEPOSIT", "issue_sent")

    def can_resume_booking(self, service):
        booking = (service.provider_snapshot or {}).get("ttbooking_booking") or {}
        return bool(
            booking.get("create_sent")
            and booking.get("request_digest")
            and not booking.get("order_token")
            and not booking.get("issue_sent")
            and not booking.get("cancel_sent")
        )

    def resume_booking(self, ctx, request):
        return self._async_book(ctx, request, resume=True)

    def booking_needs_sync(self, booking):
        return bool(booking and booking.get("order_token") and super().booking_needs_sync(booking))

    def cancel(self, ctx, locator):
        if self._config(ctx)["protocol"] == "async":
            return self._async_mutation(ctx, locator, "CANCELBOOKING", "cancel_sent")
        raise AdapterError(
            "PROTOCOL_UPGRADE_REQUIRED",
            "Для безопасной отмены TTBooking нужен async-протокол и сохранённый контекст брони",
            category="configuration",
        )

    def refund_quote(self, ctx, request):
        raise AdapterError(
            "NOT_IMPLEMENTED", "TT Booking: расчёт возврата отсутствует в stdCBS", category="configuration"
        )
