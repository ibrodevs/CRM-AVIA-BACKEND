"""HB Pro Gateway v1. Contract: https://api.hbpro.expert/docs/ (2026.09.23).

HB dictionaries and identifiers remain supplier data; never populate CRM catalogs.
"""

import hashlib
import json
import os
import re
import socket
import time
import uuid
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.fields import decrypt_value, encrypt_value
from integrations.adapters import AdapterError, ProviderAdapter
from suppliers.models import SupplierCredential

TEST_HOTELS = {
    1251539: "Casa Manzella",
    1251542: "Hilton Cologne",
    1251543: "Haus Mooren",
    1251563: "Palazzo Victoria",
    31687: "Мастер-отель Первомайская",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HotelbookAdapter(ProviderAdapter):
    key = "hotelbook"
    supported_kinds = ["hotel"]
    base_url = "https://api.hbpro.expert"
    http_timeout = 20
    search_timeout = 90
    poll_interval = 1
    page_size = 5000

    def _config(self, ctx):
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
            .order_by("id")
            .first()
        )
        if credential is None:
            credential = self._local_env_credential(ctx)
        if credential is None:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Нет активных credentials Hotelbook", category="configuration"
            )
        try:
            secrets = json.loads(credential.encrypted_secrets)
            if not isinstance(secrets, dict) or not secrets.get("login") or not secrets.get("password"):
                raise ValueError
        except (ValueError, TypeError, RuntimeError):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Некорректные credentials Hotelbook", category="configuration"
            ) from None
        if secrets.get("locale", "ru") not in ("ru", "en"):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Hotelbook locale: ru или en", category="configuration"
            )
        return credential, secrets

    def _local_env_credential(self, ctx):
        """Unsaved sandbox credential for explicit local diagnostics; DB always wins."""
        if settings.SETTINGS_MODULE not in ("config.settings.dev", "config.settings.test"):
            return None
        if not getattr(settings, "HBPRO_ALLOW_ENV_FALLBACK", False):
            return None
        from suppliers.models import Supplier

        if not Supplier.objects.filter(
            pk=ctx.supplier_id, tenant_id=ctx.tenant_id, archived_at__isnull=True
        ).exists():
            return None
        # Failed/inactive configured connections must still require verification.
        if SupplierCredential.objects.filter(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            provider_adapter=self.key,
            archived_at__isnull=True,
        ).exists():
            return None
        config = {
            "login": os.environ.get("HBPRO_LOGIN", ""),
            "password": os.environ.get("HBPRO_PASSWORD", ""),
            "locale": os.environ.get("HBPRO_LOCALE", "ru"),
            "default_citizenship": os.environ.get("HBPRO_DEFAULT_CITIZENSHIP", ""),
            "pay_form": os.environ.get("HBPRO_PAY_FORM", "CASHLESS"),
        }
        if not config["login"] or not config["password"]:
            return None
        return SupplierCredential(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"hbpro-env:{ctx.tenant_id}:{ctx.supplier_id}"),
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            provider_adapter=self.key,
            environment="sandbox",
            encrypted_secrets=json.dumps(config),
            status="active",
        )

    def verify_credentials(self, credential):
        """Authenticate via HB Pro before marking a supplier connection active."""
        from integrations.adapters import AdapterContext

        try:
            config = json.loads(credential.encrypted_secrets)
            if not isinstance(config, dict) or not config.get("login") or not config.get("password"):
                raise ValueError
        except (ValueError, TypeError):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Не заполнены реквизиты Hotelbook", category="configuration"
            ) from None
        if config.get("locale", "ru") not in ("ru", "en"):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Hotelbook locale: ru или en", category="configuration"
            )
        ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=credential.supplier_id)
        self._token(ctx, credential, config)

    def _path(self, config, path):
        return f"/api/v1/{config.get('locale', 'ru')}/{path}"

    def _cache_key(self, credential, config, name):
        fingerprint = hashlib.sha256(credential.encrypted_secrets.encode()).hexdigest()
        return f"hbpro:{credential.tenant_id}:{credential.id}:{fingerprint}:{name}"

    def _error(self, status, response, operation):
        code = (response.get("details") or {}).get("errorCode", "") if isinstance(response, dict) else ""
        if status == 401 or code.startswith("ERR013"):
            normalized, category = "AUTH_ERROR", "auth"
        elif operation == "search" and code == "ERR001#4":
            return AdapterError(
                "SEARCH_CRITERIA_INVALID",
                "Hotelbook: отели с указанными идентификаторами не найдены (ERR001#4)",
                category="validation",
            )
        elif code in ("ERR006#2", "ERR006#3"):
            normalized, category = "PRICE_CHANGED", "price"
        elif code in ("ERR006#4", "ERR006#9"):
            normalized, category = "RATE_CHANGED", "availability"
        elif code in ("ERR018#1", "ERR003#5"):
            normalized, category = "AVAILABILITY_CONFLICT", "availability"
        elif code in ("ERR005#2", "ERR006#1", "ERR009#1", "ERR014#1", "ERR014#2", "ERRH002#1", "ERRH003#1"):
            normalized, category = "RATE_UNAVAILABLE", "availability"
        elif status == 403:
            normalized, category = "PROVIDER_FORBIDDEN", "auth"
        elif status == 429:
            normalized, category = "RATE_LIMITED", "timeout"
        elif operation in ("book", "create_order", "cancel"):
            normalized, category = "BOOKING_ERROR", "booking"
        elif status == 404:
            normalized, category = "BOOKING_NOT_FOUND", "sync"
        else:
            normalized, category = "PROVIDER_ERROR", "internal"
        # Provider descriptions may echo request values; do not expose those values.
        return AdapterError(
            normalized,
            f"Hotelbook: {normalized} ({code or status})",
            category=category,
            retry_safe=operation not in ("book", "create_order", "cancel") and status >= 500,
        )

    def _wire(self, ctx, operation, method, path, payload=None, token=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        started = time.monotonic()
        response = None
        status = None
        error = None
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.http_timeout) as http:
                status = http.status
                response = json.loads(http.read(), parse_float=str)
            if not isinstance(response, (dict, list)):
                raise ValueError
        except HTTPError as exc:
            status = exc.code
            try:
                response = json.loads(exc.read(), parse_float=str)
            except (ValueError, UnicodeError):
                response = {}
            error = self._error(status, response, operation)
            if status >= 500 and operation in ("book", "create_order", "cancel"):
                error = AdapterError(
                    "BOOKING_UNKNOWN",
                    "Hotelbook: результат операции неизвестен; требуется проверка статуса",
                    category="booking_unknown",
                )
        except (TimeoutError, socket.timeout, URLError, OSError):
            mutation = operation in ("book", "create_order", "cancel")
            error = AdapterError(
                "BOOKING_UNKNOWN" if mutation else "TIMEOUT",
                "Hotelbook: нет ответа от API",
                category="booking_unknown" if mutation else "timeout",
                retry_safe=not mutation,
            )
        except (ValueError, UnicodeError):
            mutation = operation in ("book", "create_order", "cancel")
            error = AdapterError(
                "BOOKING_UNKNOWN" if mutation else "INVALID_RESPONSE",
                "Hotelbook: некорректный ответ API",
                category="booking_unknown" if mutation else "internal",
            )
        # Only transport metadata. No login/password/JWT or guest personal data in logs.
        self._log(
            ctx,
            operation,
            {
                "method": method,
                "path": re.sub(
                    r"/(results|booking_items|hotels|orders)/[^/?]+", r"/\1/{id}", path.split("?", 1)[0]
                ),
            },
            {"received": response is not None},
            result="error" if error else "success",
            error_code=error.code if error else "",
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if error:
            raise error
        return response

    def _token(self, ctx, credential, config):
        key = self._cache_key(credential, config, "token")
        cached = cache.get(key)
        if cached:
            return decrypt_value(cached)
        response = self._wire(
            ctx,
            "login",
            "POST",
            self._path(config, "gateway/login"),
            {"login": config["login"], "password": config["password"]},
        )
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise AdapterError("AUTH_ERROR", "Hotelbook: токен не получен", category="auth")
        cache.set(key, encrypt_value(token), 86340)
        return token

    def _request(self, ctx, operation, method, path, payload=None):
        credential, config = self._config(ctx)
        token = self._token(ctx, credential, config)
        try:
            return self._wire(ctx, operation, method, path, payload, token)
        except AdapterError as exc:
            if exc.code != "AUTH_ERROR":
                raise
            cache.delete(self._cache_key(credential, config, "token"))
            # A rejected JWT has not performed the requested operation; one refresh is safe.
            token = self._token(ctx, credential, config)
            return self._wire(ctx, operation, method, path, payload, token)

    def _dictionary(self, ctx, config, resource, params=None):
        credential, _ = self._config(ctx)
        key = self._cache_key(
            credential, config, f"dict:{resource}:{json.dumps(params or {}, sort_keys=True)}"
        )
        rows = cache.get(key)
        if rows is not None:
            return rows
        rows = []
        for page in range(1, 101):
            query = urlencode({**(params or {}), "itemsPerPage": 5000, "page": page})
            batch = self._request(
                ctx, "dictionary", "GET", self._path(config, f"dict/gateway/{resource}") + "?" + query
            )
            if not isinstance(batch, list):
                raise AdapterError("INVALID_RESPONSE", "Hotelbook: ожидался массив словаря")
            rows.extend(batch)
            if len(batch) < 5000:
                cache.set(key, rows, 3600)
                return rows
        raise AdapterError("INVALID_RESPONSE", "Hotelbook: превышен лимит страниц словаря")

    def _country_id(self, ctx, config, code):
        for row in self._dictionary(ctx, config, "geo/countries"):
            if not row.get("trash") and str(row.get("alpha2", "")).upper() == str(code).upper():
                return row["id"]
        raise AdapterError(
            "CITIZENSHIP_REQUIRED", "Укажите гражданство ISO alpha-2", category="configuration"
        )

    def _hotel(self, ctx, config, hotel_id):
        credential, _ = self._config(ctx)
        key = self._cache_key(credential, config, f"hotel:{hotel_id}")
        data = cache.get(key)
        if data is None:
            data = self._request(
                ctx, "hotel_details", "GET", self._path(config, f"dict/gateway/hotel/hotels/{int(hotel_id)}")
            )
            cache.set(key, data, 3600)
        return data

    def _search_payload(self, ctx, credential, config, criteria):
        try:
            check_in = date.fromisoformat(criteria["check_in"])
            check_out = date.fromisoformat(criteria["check_out"])
            if check_out <= check_in:
                raise ValueError
            rooms = criteria.get("rooms", 1)
            if isinstance(rooms, int):
                guests = int(criteria.get("guests", 2))
                if not 1 <= rooms <= 5 or guests < rooms:
                    raise ValueError
                rooms = [
                    {"adults": guests // rooms + (i < guests % rooms), "quantity": 1} for i in range(rooms)
                ]
            if not isinstance(rooms, list) or not rooms or sum(r.get("quantity", 1) for r in rooms) > 5:
                raise ValueError
            for room in rooms:
                if not 1 <= room["adults"] <= 9 or not 1 <= room.get("quantity", 1) <= 5:
                    raise ValueError
                if len(room.get("childrenAges", [])) > 4 or any(
                    not 0 <= a <= 17 for a in room.get("childrenAges", [])
                ):
                    raise ValueError
        except (KeyError, ValueError, TypeError):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Проверьте даты и состав номеров Hotelbook", category="validation"
            ) from None
        hb = criteria.get("hotelbook") or {}
        hotels = hb.get("hotels")
        location = str(criteria.get("location", "")).strip()
        if not hotels:
            hotels = [
                hotel_id
                for hotel_id, name in TEST_HOTELS.items()
                if location.casefold() in (name.casefold(), str(hotel_id))
            ]
        payload = {"checkIn": str(check_in), "checkOut": str(check_out), "rooms": rooms}
        sandbox = credential.environment in ("sandbox", "test")
        if hotels:
            try:
                hotels = [int(h) for h in hotels]
            except (TypeError, ValueError):
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Некорректные Hotelbook hotel IDs", category="validation"
                ) from None
            if sandbox and any(h not in TEST_HOTELS for h in hotels):
                raise AdapterError(
                    "TEST_HOTEL_REQUIRED", "Доступны только пять тестовых отелей", category="validation"
                )
            if not 1 <= len(hotels) <= 500:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Hotelbook: от 1 до 500 отелей", category="validation"
                )
            payload["hotels"] = hotels
        elif hb.get("city_id") and not sandbox:
            payload["cities"] = [int(hb["city_id"])]
        else:
            cities = self._dictionary(ctx, config, "geo/cities")
            matches = [
                r
                for r in cities
                if not r.get("trash") and r.get("name", "").casefold() == location.casefold()
            ]
            if len(matches) != 1:
                raise AdapterError(
                    "LOCATION_REQUIRED",
                    "Укажите название тестового отеля, его ID или однозначный город",
                    category="validation",
                )
            if sandbox:
                payload["hotels"] = [
                    h for h in TEST_HOTELS if self._hotel(ctx, config, h).get("cityId") == matches[0]["id"]
                ]
                if not payload["hotels"]:
                    raise AdapterError(
                        "TEST_HOTEL_REQUIRED",
                        "В этом городе нет доступных тестовых отелей",
                        category="availability",
                    )
            else:
                payload["cities"] = [matches[0]["id"]]
        citizenship = hb.get("citizenship_id")
        if citizenship is None:
            citizenship = self._country_id(
                ctx, config, criteria.get("citizenship") or config.get("default_citizenship", "")
            )
        payload["citizenshipId"] = int(citizenship)
        for field in ("client3Did", "confirmationMode", "refundable", "freeCancellationOnly"):
            if field in hb:
                payload[field] = hb[field]
        return payload

    def search(self, ctx, kind, criteria):
        return list(self.iter_search(ctx, kind, criteria))

    def iter_search(self, ctx, kind, criteria):
        if kind != "hotel":
            raise AdapterError(
                "UNSUPPORTED_SERVICE_KIND", "Hotelbook поддерживает только hotel", category="configuration"
            )
        credential, config = self._config(ctx)
        payload = self._search_payload(ctx, credential, config, criteria)
        start = self._request(ctx, "search", "POST", self._path(config, "hotel/gateway/search"), payload)
        if not start.get("searchId") or start.get("state") not in ("IN_PROCESS", "COMPLETED"):
            raise AdapterError("INVALID_RESPONSE", "Hotelbook: поиск не запущен")
        query_time = parse_datetime(start.get("queryTime", "")) or timezone.now()
        if timezone.is_naive(query_time):
            query_time = timezone.make_aware(query_time)
        expires_at = (query_time + timezone.timedelta(hours=4)).isoformat()
        search_id = start["searchId"]
        offset = 0
        seen = set()
        deadline = time.monotonic() + self.search_timeout
        while time.monotonic() < deadline:
            result = self._request(
                ctx,
                "search_results",
                "GET",
                f"/api/v1/hotel/gateway/results/{quote(search_id, safe='')}?offset={offset}&limit={self.page_size}",
            )
            batch = result.get("searchOffers")
            if not isinstance(batch, list) or "finished" not in result or "totalOffers" not in result:
                raise AdapterError("INVALID_RESPONSE", "Hotelbook: некорректные результаты поиска")
            offset += len(batch)
            for offer in batch:
                if offer["offerId"] in seen:
                    continue
                seen.add(offer["offerId"])
                hotel = self._hotel(ctx, config, offer["hotelId"])
                yield self._normalize(offer, search_id, expires_at, hotel, payload)
            if result["finished"] and (offset >= result["totalOffers"] or len(batch) < self.page_size):
                return
            time.sleep(self.poll_interval)
        raise AdapterError(
            "TIMEOUT", "Hotelbook: поиск не завершился вовремя", category="timeout", retry_safe=True
        )

    def _price(self, offer):
        try:
            price = offer["price"]
            amount = Decimal(str(price["amount"]))
            currency = price["currency"]
            if not amount.is_finite() or amount < 0 or not isinstance(currency, str) or len(currency) != 3:
                raise ValueError
            return {"amount": str(amount), "currency": currency}
        except (KeyError, ValueError, TypeError, InvalidOperation):
            raise AdapterError("INVALID_RESPONSE", "Hotelbook: некорректная цена") from None

    def _rules(self, offer):
        policies = offer.get("finePolicies") or offer.get("fines") or {}

        def normalized_fines(rows):
            # Search Fine.price and details Fine.amount are distinct HB schemas.
            return [{**p, "price": p.get("price") or p.get("amount") or {}} for p in (rows or [])]

        cancel = normalized_fines(policies.get("cancel"))
        text = "; ".join(
            f"{p.get('from') or 'С момента бронирования'}: {p.get('price', {}).get('amount', '?')} {p.get('price', {}).get('currency', '')}"
            for p in cancel
        )
        free_until = None
        now = timezone.now()
        penalties = [p for p in cancel if Decimal(str((p.get("price") or {}).get("amount", "0"))) > 0]
        if penalties and all(p.get("from") for p in penalties):
            dates = [parse_datetime(p["from"]) for p in penalties]
            if all(d and timezone.is_aware(d) and d > now for d in dates):
                free_until = min(dates).isoformat()
        return {
            "free_cancel_until": free_until,
            "cancel": cancel,
            "change": normalized_fines(policies.get("change")),
            "no_show": policies.get("noShow"),
            "information": offer.get("information", []),
            "cancellation_information": policies.get("info") or [],
            "additional_charges": offer.get("additionalCharges", []),
            "cancellation_rules": "; ".join(
                part for part in (text, *[str(info) for info in (policies.get("info") or [])]) if part
            )
            or "Условия отмены уточняются у поставщика",
        }

    def _normalize(self, offer, search_id, expires_at, hotel, search_request):
        rooms = offer.get("rooms") or []
        if not rooms or not all(r.get("hash") for r in rooms):
            raise AdapterError(
                "RATE_UNAVAILABLE", "Hotelbook: номера тарифа недоступны", category="availability"
            )
        details = hotel.get("details") or {}
        rules = self._rules(offer)
        quantity = sum(r.get("quantity", 1) for r in rooms)
        meal = (offer.get("meal") or {}).get("description", "")
        beds = ", ".join(b.get("name", "") for r in rooms for b in (r.get("beds") or []))
        return {
            "provider_adapter": self.key,
            "kind": "hotel",
            "external_key": "HB-" + hashlib.sha256(f"{search_id}:{offer['offerId']}".encode()).hexdigest(),
            "price": self._price(offer),
            "availability": "available" if offer.get("confirmationMode") == "ONLINE" else "on_request",
            "expires_at": expires_at,
            "itinerary": {
                "property_name": hotel.get("name") or TEST_HOTELS.get(offer["hotelId"], "Гостиница"),
                "address": details.get("address", ""),
                "city": "",
                "check_in": offer.get("checkIn"),
                "check_out": offer.get("checkOut"),
                "room": ", ".join(r.get("roomName", "") for r in rooms),
                "meal_plan": meal,
                "beds": beds,
                "rooms_count": quantity,
                "max_occupancy": sum(
                    (r.get("adults", 0) + len(r.get("children") or [])) * r.get("quantity", 1) for r in rooms
                ),
                "phone": details.get("phone", ""),
            },
            "fare": {"name": offer.get("roomGroupName") or "Тариф поставщика", **rules},
            "hotelbook": {
                "search_id": search_id,
                "offer_id": offer["offerId"],
                "hotel_id": offer["hotelId"],
                "search_request": search_request,
                "hotel": hotel,
                "offer": offer,
            },
        }

    def _terms(self, offer):
        return {
            "confirmation": offer.get("confirmationMode"),
            "fines": offer.get("finePolicies") or offer.get("fines"),
            "rooms": [
                {k: room.get(k) for k in ("hash", "quantity", "adults", "children")}
                for room in offer.get("rooms", [])
            ],
        }

    def revalidate(self, ctx, offer_snapshot):
        hb = offer_snapshot.get("hotelbook") or {}
        if not hb.get("search_id") or not hb.get("offer_id"):
            raise AdapterError(
                "RATE_UNAVAILABLE", "Нет идентификаторов предложения Hotelbook", category="availability"
            )
        expires = parse_datetime(offer_snapshot.get("expires_at", ""))
        if expires and expires <= timezone.now():
            raise AdapterError(
                "RATE_UNAVAILABLE", "Поиск Hotelbook устарел; выполните новый поиск", category="availability"
            )
        _, config = self._config(ctx)
        request = {"searchId": hb["search_id"], "offerId": hb["offer_id"]}
        for field in ("client3Did", "earlyArrivalTime", "lateDepartureTime"):
            if field in hb.get("search_request", {}):
                request[field] = hb["search_request"][field]
        offer = self._request(
            ctx, "revalidate", "POST", self._path(config, "hotel/gateway/search/details"), request
        )
        offer = {**hb.get("offer", {}), **offer}
        normalized = self._normalize(
            offer,
            hb["search_id"],
            offer_snapshot.get("expires_at"),
            hb.get("hotel", {}),
            hb.get("search_request", {}),
        )
        changed = normalized["price"] != offer_snapshot.get("price")
        return {
            "status": "price_changed" if changed else "valid",
            "terms_changed": self._terms(offer) != self._terms(hb.get("offer", {})),
            "price": normalized["price"],
            "snapshot": normalized,
            "fare_rules": self._rules(offer),
        }

    def fare_rules(self, ctx, offer_snapshot):
        return self.revalidate(ctx, offer_snapshot)["fare_rules"]

    def _guests(self, ctx, config, snapshot, passengers):
        offer = snapshot["hotelbook"]["offer"]
        check_in = date.fromisoformat(offer["checkIn"])
        prepared = []
        for person in passengers:
            try:
                born = date.fromisoformat(str(person["birth_date"]))
                age = check_in.year - born.year - ((check_in.month, check_in.day) < (born.month, born.day))
                gender = {"male": "M", "female": "F", "M": "M", "F": "F"}[person["gender"]]
                first = person.get("latin_given_name") or (
                    person.get("given_name") if offer.get("isCyrillicGuestNameAllowed") else ""
                )
                last = person.get("latin_surname") or (
                    person.get("surname") if offer.get("isCyrillicGuestNameAllowed") else ""
                )
                if not first or not last or len(first) > 50 or len(last) > 50 or age < 0:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise AdapterError(
                    "GUEST_DATA_MISSING", "Проверьте ФИО, пол и даты рождения гостей", category="validation"
                ) from None
            guest = {
                "firstName": first,
                "lastName": last,
                "gender": gender,
                "citizenship": self._country_id(ctx, config, person.get("citizenship", "")),
                "isChild": age < 18,
            }
            if age < 18:
                guest["age"] = age
            prepared.append((person.get("room_ref", ""), guest))
        result = []
        for room in offer["rooms"]:
            for _ in range(room.get("quantity", 1)):
                guests = []
                for is_child, ages in ((False, [None] * room["adults"]), (True, room.get("children") or [])):
                    for age in ages:
                        index = next(
                            (
                                i
                                for i, (ref, g) in enumerate(prepared)
                                if (not ref or ref == room["hash"])
                                and g["isChild"] == is_child
                                and (not is_child or g["age"] == age)
                            ),
                            None,
                        )
                        if index is None:
                            raise AdapterError(
                                "GUEST_ROOM_MISMATCH",
                                "Гости не соответствуют составу номеров на поиске",
                                category="validation",
                            )
                        guests.append(prepared.pop(index)[1])
                result.append({"hash": room["hash"], "guests": guests})
        if prepared:
            raise AdapterError(
                "GUEST_ROOM_MISMATCH", "Количество гостей не соответствует поиску", category="validation"
            )
        return result

    def _save_booking_snapshot(self, ctx, service_id, snapshot):
        from services.models import OrderService

        if service_id:
            OrderService.objects.filter(
                pk=service_id, tenant_id=ctx.tenant_id, supplier_id=ctx.supplier_id
            ).update(provider_snapshot=snapshot)

    def book(self, ctx, booking_request):
        from services.models import OrderService

        service_id = booking_request.get("service_id")
        snapshot = deepcopy(booking_request.get("snapshot") or {})
        if not service_id:
            raise AdapterError(
                "BOOKING_CONTEXT_REQUIRED",
                "Hotelbook бронируется через OrderService",
                category="configuration",
            )
        # Reserve the external operation durably before any mutation, also across workflows.
        with transaction.atomic():
            service = OrderService.objects.select_for_update().get(
                pk=service_id, tenant_id=ctx.tenant_id, supplier_id=ctx.supplier_id
            )
            existing = (service.provider_snapshot or {}).get("hotelbook_booking")
            if existing:
                if existing.get("state") == "complete" and all(
                    i.get("status") != "CANCELED" for i in existing.get("items", [])
                ):
                    return self._booking_result(
                        existing["locator"], existing["items"], service.provider_snapshot
                    )
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "Hotelbook: выполните status inquiry перед повтором",
                    category="booking_unknown",
                )
            if service.status in ("booked", "confirmed", "cancelled"):
                raise AdapterError(
                    "BOOKING_ERROR", "Услуга уже забронирована или отменена", category="booking"
                )
        validation = self.revalidate(ctx, snapshot)
        if validation["status"] != "valid":
            raise AdapterError(
                "PRICE_CHANGED", "Цена Hotelbook изменилась; повторите preflight", category="price"
            )
        updated = validation["snapshot"]
        old_offer = snapshot["hotelbook"]["offer"]
        new_offer = updated["hotelbook"]["offer"]
        if self._terms(old_offer) != self._terms(new_offer):
            raise AdapterError(
                "RATE_CHANGED", "Условия тарифа изменились; повторите preflight", category="availability"
            )
        _, config = self._config(ctx)
        rooms = self._guests(ctx, config, updated, booking_request.get("passengers") or [])
        ref = booking_request["client_request_id"]
        locator = "HB" + hashlib.sha256(ref.encode()).hexdigest()[:30]
        booking = {"locator": locator, "client_request_id": ref, "state": "creating_order", "items": []}
        updated["hotelbook_booking"] = booking
        with transaction.atomic():
            service = OrderService.objects.select_for_update().get(
                pk=service_id, tenant_id=ctx.tenant_id, supplier_id=ctx.supplier_id
            )
            if (service.provider_snapshot or {}).get("hotelbook_booking"):
                raise AdapterError(
                    "BOOKING_UNKNOWN", "Hotelbook: операция уже выполняется", category="booking_unknown"
                )
            # Serialize reservations for the supplier; the same concrete offer cannot
            # be booked through a second OrderService/workflow.
            from suppliers.models import Supplier

            Supplier.objects.select_for_update().get(pk=ctx.supplier_id, tenant_id=ctx.tenant_id)
            duplicate = (
                OrderService.objects.filter(
                    tenant_id=ctx.tenant_id,
                    supplier_id=ctx.supplier_id,
                    provider_snapshot__external_key=updated["external_key"],
                    provider_snapshot__hotelbook_booking__isnull=False,
                )
                .exclude(pk=service_id)
                .exists()
            )
            if duplicate:
                raise AdapterError(
                    "DUPLICATE_BOOKING",
                    "Hotelbook: это предложение уже отправлено на бронирование",
                    category="booking",
                )
            service.provider_snapshot = updated
            service.save(update_fields=["provider_snapshot", "updated_at"])
        try:
            order = self._request(
                ctx,
                "create_order",
                "POST",
                self._path(config, "order/gateway/orders"),
                {
                    "clientOrderId": ref,
                    "payForm": config.get("pay_form", "CASHLESS"),
                    "contactInfo": booking_request.get("contact_info") or config.get("contact_info") or {},
                    **(
                        {"customer": booking_request.get("customer") or config.get("customer")}
                        if new_offer.get("isEndCustomerNeeded")
                        else {}
                    ),
                },
            )
            if not order.get("orderId"):
                raise AdapterError(
                    "BOOKING_UNKNOWN", "Hotelbook: orderId не получен", category="booking_unknown"
                )
            booking.update(order_id=order["orderId"], state="booking")
            self._save_booking_snapshot(ctx, service_id, updated)
            hb = updated["hotelbook"]
            payload = {
                "searchId": hb["search_id"],
                "offerId": hb["offer_id"],
                "orderId": order["orderId"],
                "price": float(Decimal(updated["price"]["amount"])),
                "currency": updated["price"]["currency"],
                "confirmationMode": new_offer["confirmationMode"],
                "rooms": rooms,
            }
            if "client3Did" in hb["search_request"]:
                payload["client3Did"] = hb["search_request"]["client3Did"]
            response = self._request(ctx, "book", "POST", self._path(config, "hotel/gateway/book"), payload)
            items = response.get("items") or []
            if not items or not all(i.get("itemId") for i in items):
                raise AdapterError(
                    "BOOKING_UNKNOWN", "Hotelbook: части брони не получены", category="booking_unknown"
                )
            booking.update(items=items, state="complete")
            self._save_booking_snapshot(ctx, service_id, updated)
            return self._booking_result(locator, items, updated)
        except AdapterError as exc:
            booking["state"] = "unknown" if exc.category == "booking_unknown" else "failed"
            self._save_booking_snapshot(ctx, service_id, updated)
            raise

    def _booking_result(self, locator, items, snapshot=None):
        states = {i.get("status", i.get("state")) for i in items}
        if states == {"CANCELED"}:
            status = "cancelled"
        elif states == {"CONFIRMED"}:
            status = "booked"
        else:
            status = "pending"
        result = {"locator": locator, "status": status}
        if snapshot is not None:
            result["provider_snapshot"] = snapshot
        return result

    def _stored_booking(self, ctx, locator):
        from services.models import OrderService

        # A CRM-generated reference fits the existing 32-character workflow locator.
        for service in OrderService.objects.filter(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            source="api",
            provider_snapshot__hotelbook_booking__locator=locator,
        ):
            snapshot = deepcopy(service.provider_snapshot or {})
            booking = snapshot.get("hotelbook_booking") or {}
            if booking.get("locator") == locator:
                return service, snapshot, booking
        raise AdapterError(
            "BOOKING_NOT_FOUND", "Hotelbook: бронь не найдена в этом поставщике", category="sync"
        )

    def retrieve_booking(self, ctx, locator):
        service, snapshot, booking = self._stored_booking(ctx, locator)
        _, config = self._config(ctx)
        if not booking.get("order_id"):
            orders = self._request(
                ctx,
                "retrieve",
                "GET",
                self._path(config, "order/gateway/orders")
                + "?"
                + urlencode({"clientOrderId": booking["client_request_id"], "pagination": "false"}),
            )
            if not isinstance(orders, list) or len(orders) != 1:
                raise AdapterError(
                    "BOOKING_UNKNOWN", "Hotelbook: заказ требует ручной сверки", category="booking_unknown"
                )
            booking["order_id"] = orders[0]["orderId"]
            self._save_booking_snapshot(ctx, service.id, snapshot)
        order = self._request(
            ctx, "retrieve", "GET", self._path(config, f"order/gateway/orders/{booking['order_id']}")
        )
        ids = [
            i["itemId"]
            for i in order.get("items", [])
            if i.get("type") == "hotel" and i.get("serviceType") == "ONLINE"
        ]
        if not ids:
            raise AdapterError(
                "BOOKING_UNKNOWN",
                "Hotelbook: результат бронирования ещё не найден",
                category="booking_unknown",
            )
        items = [
            self._request(
                ctx,
                "retrieve_item",
                "GET",
                self._path(config, f"hotel/gateway/booking_items/{quote(i, safe='')}"),
            )
            for i in ids
        ]
        booking.update(items=items, state="complete")
        self._save_booking_snapshot(ctx, service.id, snapshot)
        return self._booking_result(locator, items, snapshot)

    def cancel(self, ctx, locator):
        self.retrieve_booking(ctx, locator)
        service, snapshot, booking = self._stored_booking(ctx, locator)
        _, config = self._config(ctx)
        for index, item in enumerate(booking["items"]):
            if item.get("status") == "CANCELED":
                continue
            if not item.get("isCancellationAllowed"):
                raise AdapterError(
                    "CANCEL_NOT_ALLOWED", "Hotelbook: отмена сейчас недоступна", category="booking"
                )
            requested = booking.setdefault("cancel_requested_ids", [])
            if item["itemId"] in requested:
                raise AdapterError(
                    "CANCEL_PENDING",
                    "Hotelbook: отмена уже отправлена; выполните status inquiry",
                    category="sync",
                )
            requested.append(item["itemId"])
            booking["state"] = "cancelling"
            self._save_booking_snapshot(ctx, service.id, snapshot)
            response = self._request(
                ctx,
                "cancel",
                "POST",
                self._path(config, f"hotel/gateway/booking_items/{quote(item['itemId'], safe='')}/cancel"),
                {},
            )
            booking["items"][index] = response
            self._save_booking_snapshot(ctx, service.id, snapshot)
        result = self._booking_result(locator, booking["items"], snapshot)
        booking["state"] = "complete" if result["status"] == "cancelled" else "cancelling"
        self._save_booking_snapshot(ctx, service.id, snapshot)
        if result["status"] != "cancelled":
            raise AdapterError(
                "CANCEL_PENDING",
                "Hotelbook: ожидается подтверждение отмены; проверьте статус через минуту",
                category="sync",
            )
        return result

    def issue(self, ctx, issue_request):
        raise AdapterError(
            "UNSUPPORTED_OPERATION", "Hotelbook не требует выписки авиабилета", category="configuration"
        )

    def refund_quote(self, ctx, request):
        raise AdapterError(
            "UNSUPPORTED_OPERATION", "Используйте штрафные политики Hotelbook", category="configuration"
        )
