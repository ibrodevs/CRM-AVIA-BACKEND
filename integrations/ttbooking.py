"""TT Booking stdCBS U1/U2/U3/T aviation adapter (docs/TTBOOKING.md)."""

import copy
import hashlib
import itertools
import json
import re
import socket
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from integrations.adapters import AdapterContext, AdapterError, AmbiguousResultError, ProviderAdapter
from suppliers.models import Supplier, SupplierCredential


class TTBookingAdapter(ProviderAdapter):
    key = "ttbooking"
    supported_kinds = ["avia"]
    base_url = "https://www.ttbooking.ru/api/scbs/json"
    http_timeout = 60

    def _credential(self, ctx, environment=None):
        if environment is None:
            metadata = (
                Supplier.objects.filter(pk=ctx.supplier_id, tenant_id=ctx.tenant_id, archived_at__isnull=True)
                .values_list("automation_capabilities", flat=True)
                .first()
                or {}
            )
            environment = metadata.get("ttbooking_environment")
        credential = (
            SupplierCredential.objects.filter(
                tenant_id=ctx.tenant_id,
                supplier_id=ctx.supplier_id,
                supplier__tenant_id=ctx.tenant_id,
                supplier__archived_at__isnull=True,
                provider_adapter=self.key,
                **({"environment": environment} if environment else {}),
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
            "password": str(raw.get("password") or raw.get("password_key") or ""),
            "environment": credential.environment,
            "_credential_id": str(credential.id),
            "_credential_version": (credential.rotated_at or credential.created_at).isoformat(),
            "_rotated_at": credential.rotated_at,
            "url": str(raw.get("url") or cls.base_url).strip(),
            "locale": str(raw.get("locale") or "ru").lower(),
        }
        if not config["agency"] or not config["user"] or not config["password"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: обязательны agency, user/login и password/password_key",
                category="configuration",
            )
        try:
            endpoint = urlsplit(config["url"])
        except ValueError:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "TT Booking: некорректный URL API", category="configuration"
            ) from None
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.fragment
        ):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: URL API должен использовать HTTPS",
                category="configuration",
            )
        if config["environment"] not in {"sandbox", "production"}:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "TT Booking: неизвестная среда", category="configuration"
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

    def _config(self, ctx, environment=None):
        credential = self._credential(ctx, environment)
        return self._parse_config(credential)

    @staticmethod
    def _account_scope(config):
        return hashlib.sha256(
            json.dumps([config[key] for key in ("url", "agency", "user", "environment")]).encode()
        ).hexdigest()

    def _config_for_snapshot(self, ctx, snapshot):
        if snapshot.get("supplier_id") and snapshot["supplier_id"] != str(ctx.supplier_id):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: данные другого поставщика", category="validation"
            )
        config = self._config(ctx, snapshot.get("provider_environment"))
        if snapshot.get("provider_account") and snapshot["provider_account"] != self._account_scope(config):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: аккаунт credentials изменился; токены прежнего аккаунта использовать нельзя",
                category="configuration",
            )
        return config

    def verify_credentials(self, credential):
        config = self._parse_config(credential)
        if not config["currency"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: у поставщика не указана валюта аккаунта",
                category="configuration",
            )
        ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=credential.supplier_id)
        # SEARCHFR is read-only; a successful, parseable provider result proves access.
        parameters = self._search_parameters(
            {
                "origin": "FRU",
                "destination": "IST",
                "date": (datetime.now(dt_timezone.utc) + timedelta(days=30)).date().isoformat(),
            }
        )
        response = self._wire(ctx, config, "SEARCHFR", parameters)
        self._normalize_search(response, config["currency"])
        return "TT Booking: read-only SEARCHFR выполнен успешно"

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
        timestamp = datetime.now(dt_timezone.utc).isoformat(timespec="seconds")
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
                error = self._unknown(command)
            elif status == 429:
                error = AdapterError(
                    "RATE_LIMITED",
                    "TT Booking: лимит запросов",
                    category="rate_limit",
                    retry_safe=not mutation,
                )
            elif 400 <= status < 500:
                error = AdapterError("INVALID_REQUEST", "TT Booking: запрос отклонён", category="validation")
            else:
                error = AdapterError("PROVIDER_ERROR", f"TT Booking: HTTP {status}", retry_safe=not mutation)
        except (TimeoutError, socket.timeout, URLError, OSError):
            error = (
                self._unknown(command)
                if mutation
                else AdapterError(
                    "TIMEOUT",
                    "TT Booking: нет ответа от API",
                    category="timeout",
                    retry_safe=True,
                )
            )
        except (ValueError, UnicodeError):
            error = (
                self._unknown(command)
                if mutation
                else AdapterError(
                    "INVALID_RESPONSE",
                    "TT Booking: некорректный JSON",
                    retry_safe=True,
                )
            )
        if error is None:
            respond = response.get("respond")
            if not isinstance(respond, dict):
                error = (
                    self._unknown(command)
                    if mutation
                    else AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует respond")
                )
            elif response.get("error") or respond.get("error") or respond.get("errors"):
                # TODO(stdCBS error schema): preserve only a safe code, never provider text/PII.
                error = (
                    self._unknown(command)
                    if mutation
                    else AdapterError("PROVIDER_ERROR", "TT Booking: API вернул ошибку")
                )
        if error is None and command in {
            "CREATEBR",
            "DISPLAYSTATUS",
            "LOADBOOKING",
            "PAYDEPOSIT",
            "DISPLAYTICKETS",
            "CANCELBOOKING",
        }:
            try:
                normalized = self._normalize_booking(response)
                if mutation and normalized["status"] == "failed":
                    code, category = {
                        "CREATEBR": ("BOOKING_ERROR", "booking"),
                        "PAYDEPOSIT": ("ISSUE_ERROR", "issue"),
                        "CANCELBOOKING": ("CANCEL_ERROR", "cancel"),
                    }[command]
                    error = AdapterError(
                        code, "TT Booking: операция отклонена поставщиком", category=category
                    )
                    error.provider_result = normalized
                elif command == "CANCELBOOKING" and normalized["status"] != "cancelled":
                    error = (
                        self._unknown(command)
                        if normalized["status"] in {"unknown", "pending"}
                        else AdapterError(
                            "CANCEL_ERROR", "TT Booking: отмена/void не подтверждены", category="cancel"
                        )
                    )
                    error.provider_result = normalized
                elif command == "CREATEBR" and normalized["status"] == "unknown":
                    error = self._unknown(command)
                    error.provider_result = normalized
            except AdapterError as exc:
                error = self._unknown(command) if mutation else exc
        if error is None and command in {"SEARCHFR", "SELECTFR"}:
            try:
                self._normalize_search(response, config["currency"])
            except AdapterError as exc:
                error = exc
        self._log(
            ctx,
            command,
            {
                "command": command,
                "environment": config["environment"],
                "credential_version": config.get("_credential_version", ""),
                "payload": self._safe_summary(payload),
            },
            self._safe_summary(response),
            result="unknown" if error and error.code.endswith("UNKNOWN") else "error" if error else "success",
            error_code=error.code if error else "",
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if error:
            if mutation and response and not getattr(error, "provider_result", None):
                booking_file = (response.get("respond") or {}).get("bookingFile")
                if (
                    isinstance(booking_file, dict)
                    and isinstance(booking_file.get("token"), str)
                    and booking_file["token"]
                ):
                    error.provider_result = {
                        "token": booking_file["token"],
                        "status": "unknown",
                        "raw_status": booking_file.get("status"),
                        "raw_snapshot": copy.deepcopy(booking_file),
                        "raw_response": copy.deepcopy(response["respond"]),
                    }
            if error.code == "AUTH_ERROR" and config.get("_credential_id"):
                SupplierCredential.objects.filter(
                    pk=config["_credential_id"],
                    tenant_id=ctx.tenant_id,
                    supplier_id=ctx.supplier_id,
                    rotated_at=config.get("_rotated_at"),
                ).update(status="failed", last_verified_at=datetime.now(dt_timezone.utc))
            raise error
        return response

    @staticmethod
    def _unknown(command):
        if command == "PAYDEPOSIT":
            return AmbiguousResultError("TT Booking: результат выписки неизвестен; требуется status inquiry")
        code, category = (
            ("BOOKING_UNKNOWN", "booking_unknown")
            if command == "CREATEBR"
            else ("CANCEL_UNKNOWN", "cancel_unknown")
        )
        return AdapterError(
            code, "TT Booking: результат операции неизвестен; требуется status inquiry", category=category
        )

    @classmethod
    def _safe_summary(cls, value, field=None):
        # Allow only protocol data that cannot identify a passenger. All auth,
        # tokens, contacts, passport fields, free-text messages and documents are omitted.
        allowed = {
            "query",
            "parameters",
            "respond",
            "flightsGroup",
            "flightGroup",
            "itineraries",
            "itinerary",
            "flights",
            "flight",
            "segments",
            "segment",
            "fares",
            "fareTotal",
            "total",
            "timeLimit",
            "eticket",
            "route",
            "seats",
            "count",
            "skipConnected",
            "eticketsOnly",
            "preferredAirlines",
            "serviceClass",
            "passengerType",
            "locationBegin",
            "locationEnd",
            "carrier",
            "code",
            "date",
            "dateBegin",
            "dateEnd",
            "bookingFile",
            "reservations",
            "reservation",
            "products",
            "airTicket",
            "status",
        }
        if isinstance(value, dict):
            return {key: cls._safe_summary(item, key) for key, item in value.items() if key in allowed}
        if isinstance(value, list):
            return [cls._safe_summary(item, field) for item in value]
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, str):
            if field == "code" and re.fullmatch(r"[A-ZА-Я0-9]{2,3}", value):
                return value
            if field in {"date", "dateBegin", "dateEnd", "timeLimit"} and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?)?", value
            ):
                return value
            enums = {
                "serviceClass": {"ECONOMY", "BUSINESS", "FIRST", "PREMIUM"},
                "passengerType": {"ADULT", "CHILD", "INFANT"},
                "status": {
                    "NEW",
                    "CANCELLED",
                    "COMPLETE",
                    "REFUND",
                    "BOOKING_NOT_CONFIRMED",
                    "EXCHANGE",
                    "UNKNOWN",
                    "TO_BE_PAYED",
                    "PAYED",
                    "TO_BE_ISSUED",
                    "ISSUING",
                    "ERROR",
                    "BOOKING",
                    "VOID_BOOKING",
                    "SELL",
                    "VOID",
                },
            }
            if value in enums.get(field, set()):
                return value
        return "[REDACTED]"

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
            raise AdapterError("INVALID_REQUEST", "TT Booking: не указана дата", category="validation")
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: некорректная дата", category="validation"
            ) from None
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
                "INVALID_REQUEST", "TT Booking: укажите аэропорты маршрута", category="validation"
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
        if any(
            seat["passengerType"] not in {"ADULT", "CHILD", "INFANT"} or seat["count"] < 0 for seat in seats
        ):
            raise AdapterError(
                "INVALID_REQUEST",
                "TT Booking: некорректные категории/количество пассажиров",
                category="validation",
            )
        seats = [seat for seat in seats if seat["count"] > 0]
        if not seats:
            raise AdapterError("INVALID_REQUEST", "TT Booking: не указаны пассажиры", category="validation")
        return seats

    def _search_parameters(self, criteria):
        cabin = str(criteria.get("cabin") or criteria.get("serviceClass") or "ECONOMY").upper()
        cabin = {"PREMIUM_ECONOMY": "PREMIUM", "PREMIUM-ECONOMY": "PREMIUM"}.get(cabin, cabin)
        if cabin not in {"ECONOMY", "BUSINESS", "FIRST", "PREMIUM"}:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: неизвестный класс обслуживания", category="validation"
            )
        return {
            "route": self._routes(criteria),
            "seats": self._seats(criteria),
            "serviceClass": cabin,
            "skipConnected": bool(criteria.get("direct_only") or criteria.get("skipConnected")),
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
            "origin": self._item(raw.get("locationBegin") or raw.get("cityBegin"))["code"],
            "destination": self._item(raw.get("locationEnd") or raw.get("cityEnd"))["code"],
            "departure": raw.get("dateBegin"),
            "arrival": raw.get("dateEnd"),
            "airline": self._item(carrier)["code"],
            "flight_number": str(raw.get("flightNumber") or ""),
            "cabin": str(raw.get("serviceClass") or "").lower(),
            "booking_class": str(raw.get("bookingClass") or ""),
            "fare_basis": str(raw.get("fareBasis") or ""),
            "baggage": (baggage.get("type") or baggage.get("value") or "")
            if isinstance(baggage, dict)
            else baggage,
            "baggage_details": copy.deepcopy(baggage),
            "departure_terminal": raw.get("terminalBegin"),
            "arrival_terminal": raw.get("terminalEnd"),
            "equipment": copy.deepcopy(raw.get("equipment")),
            "duration": raw.get("travelDuration"),
            "duration_minutes": raw.get("travelDuration"),
            "origin_country": self._item(raw.get("countryBegin"))["code"],
            "destination_country": self._item(raw.get("countryEnd"))["code"],
            "starting": raw.get("starting"),
            "connected": raw.get("connected"),
            "raw_snapshot": copy.deepcopy(raw),
            "provider_token": raw.get("token", ""),
        }

    def _normalize_search(self, response, currency):
        try:
            return self._normalize_search_result(response, currency)
        except (TypeError, ValueError, AttributeError, KeyError):
            raise AdapterError(
                "INVALID_RESPONSE", "TT Booking: некорректная структура результата поиска"
            ) from None

    def _normalize_search_result(self, response, currency):
        respond = response.get("respond") if isinstance(response, dict) else None
        if not isinstance(respond, dict):
            raise AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует respond", category="internal")
        container = respond.get("flightsGroup")
        if not isinstance(container, dict) or "flightGroup" not in container:
            raise AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует flightsGroup.flightGroup")
        search_token = respond.get("token") or ""
        if not search_token:
            raise AdapterError("PROVIDER_ERROR", "TT Booking: поиск не подтверждён (пустой token)")
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
                raise AdapterError("INVALID_RESPONSE", "TT Booking: отсутствует стоимость")
            try:
                if not Decimal(str(amount)).is_finite() or Decimal(str(amount)) < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise AdapterError("INVALID_RESPONSE", "TT Booking: некорректная стоимость") from None
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
                if (
                    not segments
                    or not search_token
                    or "token" not in group
                    or any(
                        "token" not in it or "token" not in flight
                        for it, flight in zip(itineraries, selected, strict=True)
                    )
                ):
                    raise AdapterError(
                        "INVALID_RESPONSE", "TT Booking: отсутствуют маршрут или токены выбора"
                    )
                if group.get("timeLimit"):
                    self._timestamp(group["timeLimit"])
                selected_group = copy.deepcopy(group)
                selected_group["itineraries"] = {
                    "itinerary": [
                        {**copy.deepcopy(it), "flights": {"flight": [copy.deepcopy(flight)]}}
                        for it, flight in zip(itineraries, selected, strict=True)
                    ]
                }
                first = segments[0]
                fares = group.get("fares") or {}
                fare_seats = (fares.get("fareSeats") or {}).get("fareSeat") or (
                    fares.get("fareSeats") or {}
                ).get("seat")
                offers.append(
                    {
                        "external_key": "TT-"
                        + hashlib.sha256(
                            json.dumps(
                                {"selection": selection, "segments": segments}, sort_keys=True
                            ).encode()
                        ).hexdigest(),
                        "provider_adapter": self.key,
                        "kind": "avia",
                        "price": {"amount": str(amount), "currency": currency},
                        "availability": "available",
                        "expires_at": group.get("timeLimit") or None,
                        "itinerary": {
                            "segments": segments,
                            "legs": [
                                {
                                    "segments": [
                                        self._segment(segment)
                                        for segment in self._list(
                                            (flight.get("segments") or {}).get("segment")
                                        )
                                    ]
                                }
                                for flight in selected
                            ],
                            "connections": sum(
                                max(0, len(self._list((flight.get("segments") or {}).get("segment"))) - 1)
                                for flight in selected
                            ),
                        },
                        "fare": {
                            "cabin": first.get("cabin", ""),
                            "latin_registration": group.get("latinRegistration", True),
                            "baggage": first.get("baggage", ""),
                            "booking_class": first.get("booking_class", ""),
                            "fare_basis": first.get("fare_basis", ""),
                            "eticket": bool(group.get("eticket")),
                            "gds": group.get("gds") or "",
                            "validating_carrier": self._item(group.get("carrier"))["code"],
                            "passenger_prices": self._list(fare_seats),
                        },
                        "provider_selection": selection,
                        "provider_search_token": search_token,
                        "raw_snapshot": {"token": search_token, "flightGroup": selected_group},
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
        try:
            parameters = self._search_parameters(criteria)
        except (TypeError, ValueError, AttributeError):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: некорректные параметры поиска", category="validation"
            ) from None
        offers = self._normalize_search(self._wire(ctx, config, "SEARCHFR", parameters), config["currency"])
        for offer in offers:
            offer["supplier_id"] = str(ctx.supplier_id)
            offer["provider_environment"] = config["environment"]
            offer["provider_account"] = self._account_scope(config)
        return offers

    def revalidate(self, ctx, offer_snapshot):
        try:
            return self._revalidate(ctx, offer_snapshot)
        except (TypeError, ValueError, AttributeError, KeyError, InvalidOperation):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: некорректные токены выбора или цена", category="validation"
            ) from None

    def _revalidate(self, ctx, offer_snapshot):
        if offer_snapshot.get("provider_adapter") not in (None, self.key):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: предложение другого адаптера", category="validation"
            )
        selection = offer_snapshot.get("provider_selection")
        if not isinstance(selection, dict):
            raise AdapterError(
                "OFFER_NOT_REVALIDATABLE", "TT Booking: отсутствуют токены выбора", category="validation"
            )
        if offer_snapshot.get("supplier_id") and offer_snapshot["supplier_id"] != str(ctx.supplier_id):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: предложение другого поставщика", category="validation"
            )
        groups = self._list((selection.get("flightsGroup") or {}).get("flightGroup"))
        if len(groups) != 1 or not selection.get("token") or "token" not in groups[0]:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: требуется одна группа перелётов", category="validation"
            )
        itineraries = self._list((groups[0].get("itineraries") or {}).get("itinerary"))
        if not itineraries or any(
            "token" not in it
            or len(self._list((it.get("flights") or {}).get("flight"))) != 1
            or "token" not in self._list((it.get("flights") or {}).get("flight"))[0]
            for it in itineraries
        ):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: выберите один flight на itinerary", category="validation"
            )
        # Rebuild the selection from tokens only, excluding supplier data from other branches.
        selection = {
            "token": selection["token"],
            "flightsGroup": {
                "flightGroup": [
                    {
                        "token": groups[0]["token"],
                        "itineraries": {
                            "itinerary": [
                                {
                                    "token": it["token"],
                                    "flights": {
                                        "flight": [{"token": self._list(it["flights"]["flight"])[0]["token"]}]
                                    },
                                }
                                for it in itineraries
                            ]
                        },
                    }
                ]
            },
        }
        config = self._config_for_snapshot(ctx, offer_snapshot)
        if (
            offer_snapshot.get("provider_environment")
            and offer_snapshot["provider_environment"] != config["environment"]
        ):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "TT Booking: среда credentials не соответствует предложению",
                category="configuration",
            )
        offers = self._normalize_search(self._wire(ctx, config, "SELECTFR", selection), config["currency"])
        if not offers:
            raise AdapterError(
                "AVAILABILITY_CONFLICT", "TT Booking: тариф недоступен", category="availability"
            )
        if len(offers) != 1:
            raise AdapterError("INVALID_RESPONSE", "TT Booking: SELECTFR вернул несколько предложений")
        offer = offers[0]
        offer["supplier_id"] = str(ctx.supplier_id)
        offer["provider_environment"] = config["environment"]
        offer["provider_account"] = self._account_scope(config)
        old = offer_snapshot.get("price") or {}
        changed = old.get("currency") != offer["price"]["currency"] or Decimal(
            str(old.get("amount", "0"))
        ) != Decimal(offer["price"]["amount"])
        return {
            "status": "price_changed" if changed else "valid",
            "price_changed": changed,
            "price": offer["price"],
            "snapshot": offer,
        }

    def fare_rules(self, ctx, offer_snapshot):
        self._log(
            ctx,
            "fare_rules",
            {"selection_present": bool(offer_snapshot.get("provider_selection"))},
            {"raw_available": False},
            result="unavailable",
            error_code="NOT_IMPLEMENTED",
            http_status=None,
        )
        # §3.8 describes readFlightRules with legacy password authentication, without a
        # stdCBS context.command. Never invent a command or send that legacy password.
        return {
            "rules": [],
            "refund": "",
            "exchange": "",
            "no_show": "",
            "raw_available": False,
            "unavailable_reason": "TODO: TT Booking должен подтвердить stdCBS-команду readFlightRules и доступ аккаунта",
        }

    @staticmethod
    def normalize_fare_rules(response):
        """Normalize documented §3.8 remarks without interpreting ATPCO text."""
        groups = TTBookingAdapter._list(
            (response.get("respond", {}).get("remarkGroups") or {}).get("remarkGroup")
        )
        rules = [
            copy.deepcopy(remark)
            for group in groups
            for remark in TTBookingAdapter._list((group.get("remarks") or {}).get("remark"))
        ]
        return {"rules": rules, "refund": "", "exchange": "", "no_show": "", "raw_available": bool(rules)}

    @staticmethod
    def _phone(contact):
        # stdCBS requires separate country/area/subscriber parts. Do not guess a
        # numbering plan from a national number. Accept explicit parts or E.164.
        country = str(contact.get("countryCode") or contact.get("country_code") or "")
        area = str(contact.get("areaCode") or contact.get("area_code") or "")
        number = str(contact.get("phoneNumber") or contact.get("phone") or "")
        if country and area and number:
            return {"countryCode": country, "areaCode": area, "phone": number}
        import phonenumbers

        try:
            if not number.startswith("+"):
                raise ValueError
            parsed = phonenumbers.parse(number, None)
            if not phonenumbers.is_valid_number(parsed) or parsed.extension:
                raise ValueError
            national = phonenumbers.national_significant_number(parsed)
            length = phonenumbers.length_of_national_destination_code(parsed)
            return {
                "countryCode": str(parsed.country_code),
                "areaCode": national[:length],
                "phone": national[length:],
            }
        except (ValueError, phonenumbers.NumberParseException):
            raise AdapterError(
                "INVALID_REQUEST",
                "TT Booking: укажите телефон в E.164 или countryCode, areaCode, phone",
                category="validation",
            ) from None

    def _booking_parameters(self, snapshot, request):
        selection = copy.deepcopy(snapshot.get("provider_selection") or {})
        if not selection:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: отсутствуют токены выбранного тарифа", category="validation"
            )
        contact = request.get("contact_info") or request.get("customer") or {}
        phone = self._phone(contact)
        if not contact.get("name") or not contact.get("email"):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: нужны имя и e-mail контакта", category="validation"
            )
        customer = {"name": contact["name"], "email": contact["email"], **phone}
        departure = (snapshot.get("itinerary") or {}).get("segments", [{}])[0].get("departure")
        trip_date = datetime.fromisoformat(departure).date() if departure else None
        passengers = []
        for index, person in enumerate(request.get("passengers") or []):
            doc = person.get("document") or {}
            birthday = person.get("birth_date")
            if not birthday or not trip_date:
                raise AdapterError(
                    "INVALID_REQUEST", "TT Booking: нужны дата рождения и дата вылета", category="validation"
                )
            born = datetime.fromisoformat(str(birthday)).date()
            age = trip_date.year - born.year - ((trip_date.month, trip_date.day) < (born.month, born.day))
            category = "ADULT" if age >= 12 else "CHILD" if age >= 2 else "INFANT"
            gender = str(person.get("gender") or "").upper()
            country = person.get("citizenship") or ""
            latin = (snapshot.get("fare") or {}).get("latin_registration", True)
            first = person.get("latin_given_name") if latin else person.get("given_name")
            last = person.get("latin_surname") if latin else person.get("surname")
            middle = person.get("latin_middle_name") if latin else person.get("middle_name")
            document_type = doc.get("provider_type")
            if not document_type:
                document_type = {
                    "birth_certificate": "BIRTHDAY_NOTIFICATION",
                    "national_passport": "INTERNAL" if country == "RU" else "PASSPORT",
                    "foreign_passport": "FOREIGN" if country == "RU" else "PASSPORT",
                }.get(doc.get("type"))
            number = str(doc.get("series") or "") + str(doc.get("number") or "")
            number = number.replace(" ", "")
            if (
                not first
                or not last
                or gender not in {"MALE", "FEMALE"}
                or not country
                or not number
                or document_type not in {"INTERNAL", "FOREIGN", "BIRTHDAY_NOTIFICATION", "PASSPORT"}
            ):
                raise AdapterError(
                    "INVALID_REQUEST",
                    "TT Booking: неполные данные пассажира или неподдерживаемый документ",
                    category="validation",
                )
            if (document_type == "INTERNAL" and (len(number) != 10 or not number.isdigit())) or (
                document_type == "FOREIGN" and (len(number) != 9 or not number.isdigit())
            ):
                raise AdapterError(
                    "INVALID_REQUEST",
                    "TT Booking: неверный формат номера документа РФ",
                    category="validation",
                )
            if not doc.get("expires_at"):
                raise AdapterError(
                    "INVALID_REQUEST",
                    "TT Booking: задайте срок действия документа (включая бессрочный документ)",
                    category="validation",
                )
            passport = {
                "firstName": first,
                "lastName": last,
                "middleName": middle or "",
                "citizenship": self._item(country),
                "issued": self._date(doc["issued_at"]) if doc.get("issued_at") else "",
                "expired": self._date(doc["expires_at"]),
                "number": number,
                "type": document_type,
                "birthday": self._date(birthday),
                "gender": gender,
            }
            if datetime.fromisoformat(str(doc["expires_at"])).date() < trip_date or age < 0:
                raise AdapterError(
                    "INVALID_REQUEST",
                    "TT Booking: неверная дата рождения или истёкший документ",
                    category="validation",
                )
            passenger_phone = (
                self._phone(person)
                if person.get("phone")
                else phone
                if index == 0
                else {"countryCode": "", "areaCode": "", "phone": ""}
            )
            passengers.append(
                {
                    "passport": passport,
                    "type": category,
                    "phoneType": "HOME_PHONE",
                    "phoneNumber": passenger_phone["phone"],
                    "countryCode": passenger_phone["countryCode"],
                    "areaCode": passenger_phone["areaCode"],
                }
            )
        if not passengers:
            raise AdapterError("INVALID_REQUEST", "TT Booking: не указаны пассажиры", category="validation")
        expected = {}
        for row in (snapshot.get("fare") or {}).get("passenger_prices") or []:
            expected[row.get("passengerType")] = expected.get(row.get("passengerType"), 0) + int(
                row.get("count") or 0
            )
        actual = {}
        for person in passengers:
            actual[person["type"]] = actual.get(person["type"], 0) + 1
        if expected and expected != actual:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: пассажиры не соответствуют поиску", category="validation"
            )
        return {**selection, "customer": customer, "passengers": {"passenger": passengers}}

    def _normalize_booking(self, response):
        try:
            booking = response["respond"]["bookingFile"]
            if (
                not isinstance(booking, dict)
                or not isinstance(booking.get("token"), str)
                or not booking["token"]
            ):
                raise ValueError
            reservations = self._list((booking.get("reservations") or {}).get("reservation"))
            products = [
                (reservation, product)
                for reservation in reservations
                for product in self._list((reservation.get("products") or {}).get("airTicket"))
            ]
            tickets = []
            for reservation, product in products:
                number = str(product.get("number") or "")
                if not number:
                    continue
                segments = self._list((product.get("segments") or {}).get("segment"))
                if not segments:
                    segments = [
                        segment
                        for it in self._list((product.get("itineraries") or {}).get("itinerary"))
                        for flight in self._list((it.get("flights") or {}).get("flight"))
                        for segment in self._list((flight.get("segments") or {}).get("segment"))
                    ]
                if not self._item(product.get("carrier"))["code"] or len(number) > 20:
                    raise ValueError
                if product.get("issueDate"):
                    self._timestamp(product["issueDate"])
                tickets.append(
                    {
                        "ticket_number": number,
                        "carrier": self._item(product.get("carrier"))["code"],
                        "passenger": copy.deepcopy(product.get("passenger") or {}),
                        "segments": [self._segment(seg) for seg in segments],
                        "booking_locator": product.get("recordLocator")
                        or reservation.get("recordLocator")
                        or "",
                        "status": {
                            "SELL": "issued",
                            "VOID": "void",
                            "REFUND": "refunded",
                            "EXCHANGE": "exchanged",
                        }.get(product.get("status"), "unknown"),
                        "raw_status": product.get("status"),
                        "issued_at": product.get("issueDate"),
                        "raw_snapshot": copy.deepcopy(product),
                    }
                )
            raw_status = booking.get("status")
            status = {
                "NEW": "booked",
                "PAYED": "booked",
                "CANCELLED": "cancelled",
                "COMPLETE": "issued",
                "ISSUING": "issued",
                "BOOKING_NOT_CONFIRMED": "pending",
                "TO_BE_PAYED": "pending",
                "TO_BE_ISSUED": "pending",
                "ERROR": "failed",
                "REFUND": "refunded",
                "EXCHANGE": "exchanged",
            }.get(raw_status, "unknown")
            if status == "cancelled" and any(
                product.get("status") not in {"VOID", "VOID_BOOKING", "REFUND"} for _, product in products
            ):
                status = "unknown"
            if products and all(
                product.get("status") == "SELL" and product.get("number") for _, product in products
            ):
                status = "issued" if raw_status != "CANCELLED" else "unknown"
            if status == "booked" and any(
                product.get("status") not in {"BOOKING", "SELL"} for _, product in products
            ):
                status = "pending"
            deadlines = [row.get("timeLimit") for row in reservations if row.get("timeLimit")]
            deadline = min(deadlines, key=self._timestamp) if deadlines else None
            locators = [row.get("recordLocator") for row in reservations if row.get("recordLocator")]
            return {
                "token": booking["token"],
                "locator": locators[0] if locators else "",
                "record_locators": locators,
                "reservation_tokens": [row.get("token") for row in reservations],
                "reg_locators": [row.get("regLocator") for row in reservations],
                "status": status,
                "raw_status": raw_status,
                "ticketing_deadline": deadline,
                "create_date": booking.get("createDate"),
                "tickets": tickets,
                # TODO(stdCBS documents): the document collection schema is unspecified; retain exact data.
                "documents": copy.deepcopy(booking.get("documents") or {}),
                "raw_snapshot": copy.deepcopy(booking),
                "raw_response": copy.deepcopy(response["respond"]),
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AdapterError("INVALID_RESPONSE", "TT Booking: некорректный bookingFile") from None

    @staticmethod
    def _timestamp(value):
        try:
            timestamp = datetime.fromisoformat(str(value))
            if timestamp.tzinfo is None:
                raise ValueError
            return timestamp
        except (ValueError, TypeError):
            raise AdapterError(
                "INVALID_RESPONSE", "TT Booking: timeLimit без корректного часового пояса"
            ) from None

    def _service(self, ctx, reference, *, lock=False):
        from django.db.models import Q

        from services.models import OrderService

        queryset = OrderService.objects.filter(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            archived_at__isnull=True,
            kind="avia",
            provider_snapshot__provider_adapter=self.key,
        )
        if lock:
            queryset = queryset.select_for_update()
        if isinstance(reference, dict) and reference.get("service_id"):
            queryset = queryset.filter(pk=reference["service_id"])
        elif isinstance(reference, str) and reference:
            queryset = queryset.filter(
                Q(external_id=reference) | Q(provider_snapshot__ttbooking_booking__token=reference)
            )
        else:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: нужны service_id или bookingFile.token", category="validation"
            )
        rows = list(queryset[:2])
        if len(rows) != 1:
            raise AdapterError(
                "BOOKING_NOT_FOUND" if not rows else "INVALID_REQUEST",
                "TT Booking: бронь не найдена однозначно у этого поставщика",
                category="validation",
            )
        return rows[0]

    def _save_booking(self, ctx, service_id, booking):
        from django.db import transaction

        with transaction.atomic():
            service = self._service(ctx, {"service_id": service_id}, lock=True)
            snapshot = copy.deepcopy(service.provider_snapshot or {})
            snapshot["ttbooking_booking"] = {**snapshot.get("ttbooking_booking", {}), **booking}
            service.provider_snapshot = snapshot
            service.save(update_fields=["provider_snapshot", "updated_at"])
        return snapshot

    @staticmethod
    def _booking_result(booking, snapshot=None):
        result = {
            key: booking.get(key)
            for key in (
                "locator",
                "status",
                "raw_status",
                "ticketing_deadline",
                "tickets",
                "documents",
                "record_locators",
            )
        }
        if snapshot is not None:
            result["provider_snapshot"] = snapshot
        return result

    def book(self, ctx, booking_request):
        from django.db import transaction

        service = self._service(ctx, booking_request)
        snapshot = copy.deepcopy(service.provider_snapshot or {})
        existing = snapshot.get("ttbooking_booking")
        if existing:
            if existing.get("state") == "complete" and existing.get("status") in {
                "booked",
                "issued",
                "pending",
            }:
                return self._booking_result(existing, snapshot)
            raise AdapterError(
                "BOOKING_UNKNOWN",
                "TT Booking: предыдущая попытка требует status inquiry",
                category="booking_unknown",
            )
        validation = self.revalidate(ctx, snapshot)
        if validation["status"] != "valid":
            raise AdapterError(
                "PRICE_CHANGED",
                "TT Booking: цена изменилась; повторите preflight и подтвердите цену",
                category="price",
            )
        selected = validation["snapshot"]
        if selected.get("expires_at") and self._timestamp(selected["expires_at"]) <= datetime.now(
            dt_timezone.utc
        ):
            raise AdapterError("OFFER_EXPIRED", "TT Booking: срок предложения истёк", category="availability")
        try:
            parameters = self._booking_parameters(selected, booking_request)
        except (TypeError, ValueError, AttributeError):
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: некорректные данные пассажиров", category="validation"
            ) from None
        config = self._config_for_snapshot(ctx, selected)
        with transaction.atomic():
            service = self._service(ctx, booking_request, lock=True)
            if (service.provider_snapshot or {}).get("ttbooking_booking"):
                raise AdapterError(
                    "BOOKING_UNKNOWN", "TT Booking: бронирование уже отправлено", category="booking_unknown"
                )
            selected["ttbooking_booking"] = {
                "state": "sending",
                "client_request_id": booking_request.get("client_request_id"),
            }
            service.provider_snapshot = selected
            service.save(update_fields=["provider_snapshot", "updated_at"])
        try:
            response = self._wire(
                ctx,
                config,
                "CREATEBR",
                parameters,
                mutation=True,
            )
            booking = self._normalize_booking(response)
            booking["state"] = "complete"
            updated = self._save_booking(ctx, service.id, booking)
            if booking["status"] not in {"booked", "issued", "pending"}:
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "TT Booking: статус бронирования не подтверждён",
                    category="booking_unknown",
                )
            return self._booking_result(booking, updated)
        except AdapterError as exc:
            if getattr(exc, "provider_result", None):
                self._save_booking(ctx, service.id, exc.provider_result)
            self._save_booking(
                ctx, service.id, {"state": "unknown" if exc.category == "booking_unknown" else "failed"}
            )
            raise

    def retrieve_booking(self, ctx, locator, *, refresh=False):
        service = self._service(ctx, locator)
        stored = (service.provider_snapshot or {}).get("ttbooking_booking", {})
        token = stored.get("token")
        if not token:
            # U3 has no documented client request lookup. Never re-create after a lost CREATEBR result.
            raise AdapterError(
                "BOOKING_UNKNOWN",
                "TT Booking: bookingFile.token не получен; требуется сверка у поставщика",
                category="booking_unknown",
            )
        needs_refresh = (
            refresh
            or stored.get("state") in {"sending", "unknown"}
            or stored.get("issue_state") in {"sending", "unknown"}
            or stored.get("cancel_state") in {"sending", "unknown"}
        )
        response = self._wire(
            ctx,
            self._config_for_snapshot(ctx, service.provider_snapshot),
            "LOADBOOKING" if needs_refresh else "DISPLAYSTATUS",
            {"token": token},
        )
        booking = self._normalize_booking(response)
        if booking["status"] == "issued" or stored.get("issue_state") in {"sending", "unknown", "complete"}:
            tickets = self.display_tickets(ctx, token)
            booking["tickets"] = tickets["tickets"]
            if tickets["tickets"] and all(ticket["status"] == "issued" for ticket in tickets["tickets"]):
                booking["status"] = "issued"
            if booking["status"] == "issued" and not tickets["tickets"]:
                booking["status"] = "unknown"
                booking["issue_state"] = "unknown"
            elif booking["status"] == "issued":
                booking["issue_state"] = "complete"
                booking["ticket_inventory"] = [
                    [row["carrier"], row["ticket_number"]] for row in tickets["tickets"]
                ]
            elif (
                needs_refresh
                and stored.get("issue_state") == "unknown"
                and booking["status"] == "booked"
                and booking["raw_status"] == "NEW"
                and not tickets["tickets"]
            ):
                booking["issue_state"] = "ready"
        if stored.get("issue_state") == "complete" and booking["status"] not in {
            "issued",
            "cancelled",
            "refunded",
            "exchanged",
        }:
            booking["status"] = "unknown"
        if booking["status"] == "cancelled":
            expected = {tuple(key) for key in stored.get("ticket_inventory", [])} or {
                (row["carrier"], row["ticket_number"])
                for row in stored.get("tickets", [])
                if row.get("status") == "issued"
            }
            voided = {
                (row["carrier"], row["ticket_number"])
                for row in booking["tickets"]
                if row["status"] in {"void", "refunded"}
            }
            if expected and not expected.issubset(voided):
                booking["status"] = "unknown"
                booking["cancel_state"] = "unknown"
            else:
                booking["cancel_state"] = "complete"
        elif (
            needs_refresh
            and stored.get("cancel_state") == "unknown"
            and booking["status"] in {"booked", "issued"}
        ):
            booking["cancel_state"] = "ready"
        if booking["status"] in {"booked", "issued", "cancelled"}:
            booking["state"] = "complete"
        snapshot = self._save_booking(ctx, service.id, booking)
        return self._booking_result(booking, snapshot)

    def display_tickets(self, ctx, token):
        if not isinstance(token, str) or not token:
            raise AdapterError(
                "INVALID_REQUEST", "TT Booking: требуется bookingFile.token", category="validation"
            )
        snapshot = self._service(ctx, token).provider_snapshot
        return self._normalize_booking(
            self._wire(ctx, self._config_for_snapshot(ctx, snapshot), "DISPLAYTICKETS", {"token": token})
        )

    def issue(self, ctx, issue_request):
        from django.db import transaction

        service = self._service(
            ctx,
            issue_request
            if issue_request.get("service_id")
            else issue_request.get("token") or issue_request.get("locator"),
        )
        booking = (service.provider_snapshot or {}).get("ttbooking_booking", {})
        if booking.get("issue_state") in {"sending", "unknown"}:
            raise AmbiguousResultError("TT Booking: выполните status inquiry перед повтором выписки")
        current = self.retrieve_booking(ctx, {"service_id": str(service.id)}, refresh=True)
        service.refresh_from_db()
        token = service.provider_snapshot["ttbooking_booking"].get("token")
        tickets = self.display_tickets(ctx, token)
        if tickets["tickets"]:
            if current["status"] == "issued" and all(row["status"] == "issued" for row in tickets["tickets"]):
                booking = {
                    **service.provider_snapshot["ttbooking_booking"],
                    "tickets": tickets["tickets"],
                    "issue_state": "complete",
                }
                return self._booking_result(booking, self._save_booking(ctx, service.id, booking))
            raise AmbiguousResultError("TT Booking: уже есть билеты; повтор выписки запрещён")
        if current["status"] == "issued" or current.get("raw_status") in {"COMPLETE", "ISSUING"}:
            raise AmbiguousResultError("TT Booking: выписка подтверждена, номера билетов ещё не получены")
        if current["status"] != "booked" or service.provider_snapshot["ttbooking_booking"].get(
            "raw_status"
        ) not in {"NEW"}:
            raise AdapterError("ISSUE_ERROR", "TT Booking: статус не разрешает выписку", category="issue")
        if not current.get("ticketing_deadline") or self._timestamp(
            current["ticketing_deadline"]
        ) <= datetime.now(dt_timezone.utc):
            raise AdapterError(
                "OFFER_EXPIRED", "TT Booking: отсутствует или истёк срок выписки", category="availability"
            )
        with transaction.atomic():
            locked = self._service(ctx, {"service_id": str(service.id)}, lock=True)
            stored = locked.provider_snapshot.get("ttbooking_booking", {})
            if stored.get("issue_state") in {"sending", "unknown", "complete"} or stored.get(
                "cancel_state"
            ) in {"sending", "unknown", "complete"}:
                raise AmbiguousResultError("TT Booking: выписка уже выполняется или завершена")
            snapshot = copy.deepcopy(locked.provider_snapshot)
            snapshot["ttbooking_booking"]["issue_state"] = "sending"
            locked.provider_snapshot = snapshot
            locked.save(update_fields=["provider_snapshot", "updated_at"])
        try:
            response = self._wire(
                ctx,
                self._config_for_snapshot(ctx, service.provider_snapshot),
                "PAYDEPOSIT",
                {"token": token},
                mutation=True,
            )
            booking = self._normalize_booking(response)
            self._save_booking(ctx, service.id, booking)
            fetched = self.display_tickets(ctx, token)
            booking["tickets"] = fetched["tickets"]
            if (
                booking["status"] != "issued"
                or not booking["tickets"]
                or any(row["status"] != "issued" for row in booking["tickets"])
            ):
                raise AmbiguousResultError("TT Booking: выписка не подтверждена; требуется status inquiry")
            booking["issue_state"] = "complete"
            booking["ticket_inventory"] = [
                [row["carrier"], row["ticket_number"]] for row in booking["tickets"]
            ]
            return self._booking_result(booking, self._save_booking(ctx, service.id, booking))
        except AdapterError as exc:
            if getattr(exc, "provider_result", None):
                self._save_booking(ctx, service.id, exc.provider_result)
            # Once PAYDEPOSIT was sent, a failure reading tickets is also ambiguous.
            if exc.code in {"TIMEOUT", "INVALID_RESPONSE", "PROVIDER_ERROR"}:
                exc = AmbiguousResultError("TT Booking: нет подтверждения выписки; требуется status inquiry")
            self._save_booking(
                ctx, service.id, {"issue_state": "unknown" if exc.category == "issue_unknown" else "failed"}
            )
            raise exc

    def cancel(self, ctx, locator):
        from django.db import transaction

        service = self._service(ctx, locator)
        stored = (service.provider_snapshot or {}).get("ttbooking_booking", {})
        if stored.get("cancel_state") in {"sending", "unknown", "complete"}:
            self.retrieve_booking(ctx, {"service_id": str(service.id)}, refresh=True)
            service.refresh_from_db()
            booking = service.provider_snapshot["ttbooking_booking"]
            if booking.get("status") == "cancelled":
                return self._booking_result(booking, service.provider_snapshot)
            raise AdapterError(
                "CANCEL_UNKNOWN",
                "TT Booking: отмена не подтверждена; требуется status inquiry",
                category="cancel_unknown",
            )
        current = self.retrieve_booking(ctx, {"service_id": str(service.id)}, refresh=True)
        if current["status"] == "cancelled":
            return current
        if current["status"] not in {"booked", "issued"}:
            raise AdapterError("CANCEL_ERROR", "TT Booking: статус не позволяет отмену", category="cancel")
        with transaction.atomic():
            locked = self._service(ctx, {"service_id": str(service.id)}, lock=True)
            if locked.provider_snapshot["ttbooking_booking"].get("cancel_state") in {
                "sending",
                "unknown",
                "complete",
            } or locked.provider_snapshot["ttbooking_booking"].get("issue_state") in {"sending", "unknown"}:
                raise AdapterError(
                    "CANCEL_UNKNOWN", "TT Booking: отмена уже выполняется", category="cancel_unknown"
                )
            snapshot = copy.deepcopy(locked.provider_snapshot)
            snapshot["ttbooking_booking"]["cancel_state"] = "sending"
            locked.provider_snapshot = snapshot
            locked.save(update_fields=["provider_snapshot", "updated_at"])
        token = snapshot["ttbooking_booking"]["token"]
        try:
            booking = self._normalize_booking(
                self._wire(
                    ctx,
                    self._config_for_snapshot(ctx, snapshot),
                    "CANCELBOOKING",
                    {"token": token},
                    mutation=True,
                )
            )
            if booking["status"] != "cancelled":
                raise AdapterError(
                    "CANCEL_ERROR", "TT Booking: поставщик не подтвердил отмену/void", category="cancel"
                )
            if current["status"] == "issued":
                expected = {(row["carrier"], row["ticket_number"]) for row in current["tickets"]}
                voided = {
                    (row["carrier"], row["ticket_number"])
                    for row in booking["tickets"]
                    if row["status"] in {"void", "refunded"}
                }
                if not expected.issubset(voided):
                    booking["tickets"] = self.display_tickets(ctx, token)["tickets"]
                    voided = {
                        (row["carrier"], row["ticket_number"])
                        for row in booking["tickets"]
                        if row["status"] in {"void", "refunded"}
                    }
                    if not expected.issubset(voided):
                        booking["status"] = "unknown"
                        self._save_booking(ctx, service.id, booking)
                        raise AdapterError(
                            "CANCEL_UNKNOWN",
                            "TT Booking: void всех билетов не подтверждён; требуется status inquiry",
                            category="cancel_unknown",
                        )
            booking["cancel_state"] = "complete"
            return self._booking_result(booking, self._save_booking(ctx, service.id, booking))
        except AdapterError as exc:
            if getattr(exc, "provider_result", None):
                self._save_booking(ctx, service.id, exc.provider_result)
            self._save_booking(
                ctx, service.id, {"cancel_state": "unknown" if exc.category == "cancel_unknown" else "failed"}
            )
            raise

    def refund_quote(self, ctx, request):
        raise AdapterError(
            "NOT_IMPLEMENTED", "TT Booking: расчёт возврата отсутствует в stdCBS", category="configuration"
        )
