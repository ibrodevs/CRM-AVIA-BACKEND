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
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.fields import decrypt_value, encrypt_value
from integrations.adapters import AdapterError, ProviderAdapter
from integrations.hotelbook_lock import booking_lease
from integrations.hotelbook_observability import external_ids, normalized_status, private_values, sanitize
from suppliers.models import SupplierCredential

TEST_HOTELS = {
    1251201: "Occidental Praha Wilson Hotel (ОТЕЛЬ ДЛЯ ТЕСТОВ!)",
    1251539: "Casa Manzella",
    1251542: "Hilton Cologne",
    1251543: "Haus Mooren",
    1251563: "Palazzo Victoria",
    131687: "Мастер-отель Первомайская",
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
    revalidation_ttl = 30
    dictionary_ttl = 86400

    def _config(self, ctx):
        credential = (
            SupplierCredential.objects.filter(
                tenant_id=ctx.tenant_id,
                supplier_id=ctx.supplier_id,
                supplier__tenant_id=ctx.tenant_id,
                supplier__archived_at__isnull=True,
                provider_adapter=self.key,
                environment=getattr(settings, "HBPRO_ENVIRONMENT", "sandbox"),
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
        ctx.environment = credential.environment
        return credential, secrets

    def _local_env_credential(self, ctx):
        """Unsaved sandbox credential for explicit local diagnostics; DB always wins."""
        if settings.SETTINGS_MODULE not in ("config.settings.dev", "config.settings.test"):
            return None
        if getattr(settings, "HBPRO_ENVIRONMENT", "sandbox") != "sandbox":
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
        ctx.environment = credential.environment
        self._token(ctx, credential, config)

    def _path(self, config, path):
        return f"/api/v1/{config.get('locale', 'ru')}/{path}"

    def _cache_key(self, credential, config, name):
        fingerprint = hashlib.sha256(credential.encrypted_secrets.encode()).hexdigest()
        return f"hbpro:{credential.tenant_id}:{credential.id}:{fingerprint}:{name}"

    def _error(self, status, response, operation):
        details = response.get("details") if isinstance(response, dict) else None
        code = str(details.get("errorCode") or "") if isinstance(details, dict) else ""
        if not re.fullmatch(r"[A-Z0-9#_-]{1,64}", code):
            code = ""
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
        elif status >= 500:
            normalized, category = "SUPPLIER_UNAVAILABLE", "internal"
        elif operation == "cancel":
            normalized, category = "CANCELLATION_FAILED", "booking"
        elif status in (400, 422) and operation not in ("book", "create_order"):
            normalized, category = "INVALID_REQUEST", "validation"
        elif operation == "book":
            normalized, category = "BOOKING_REJECTED", "booking"
        elif operation == "create_order":
            normalized, category = "INVALID_REQUEST", "validation"
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

    def _wire(self, ctx, operation, method, path, payload=None, token=None, secrets=()):
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
            collection = operation in ("dictionary", "search_orders")
            if not isinstance(response, list if collection else dict):
                raise ValueError
            expected = {
                "login": {"token": str},
                "search": {"searchId": str, "state": str},
                "search_results": {"searchOffers": list, "finished": bool, "totalOffers": int},
                "create_order": {"orderId": int},
                "book": {"items": list},
                "order_info": {"orderId": int, "items": list},
                "retrieve_item": {"itemId": str, "status": str},
                "cancel": {"itemId": str, "status": str},
            }.get(operation, {})
            if any(not isinstance(response.get(k), kind) for k, kind in expected.items()):
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
        from integrations.models import IntegrationLog

        sensitive = (
            list(secrets) + private_values(payload) + private_values(response) + ([token] if token else [])
        )
        ids = external_ids(payload, response)
        match = re.search(r"/(results|booking_items|orders)/([^/?]+)", path)
        if match:
            key = {"results": "searchId", "booking_items": "itemId", "orders": "orderId"}[match[1]]
            ids.setdefault(key, [match[2]])
        statuses = []

        def collect_states(value):
            if isinstance(value, dict):
                if isinstance(value.get("status"), str):
                    statuses.append(value["status"])
                for v in value.values():
                    collect_states(v)
            elif isinstance(value, list):
                for v in value:
                    collect_states(v)

        collect_states(response)
        IntegrationLog.objects.create(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            correlation_id=ctx.correlation_id,
            order_id=ctx.order_id,
            certification_run_id=ctx.certification_run_id,
            provider_adapter=self.key,
            environment=ctx.environment,
            operation=operation,
            http_method=method,
            url=sanitize(self.base_url + path, sensitive),
            request_headers=sanitize(headers, sensitive),
            request_sanitized=sanitize(payload, sensitive),
            response_sanitized=sanitize(response, sensitive),
            external_ids=sanitize(ids, sensitive),
            supplier_status=",".join(sorted(set(statuses)))[:255],
            provider_error=sanitize(response, sensitive) if error else None,
            raw_error=sanitize(str(error), sensitive) if error else "",
            error_code=error.code if error else "",
            result="error" if error else "success",
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
        with transaction.atomic():
            SupplierCredential.objects.select_for_update().filter(pk=credential.pk).first()
            cached = cache.get(key)
            if cached:
                return decrypt_value(cached)
            error = None
            try:
                response = self._wire(
                    ctx,
                    "login",
                    "POST",
                    self._path(config, "gateway/login"),
                    {"login": config["login"], "password": config["password"]},
                )
            except AdapterError as exc:
                # Commit the sanitized failure log before propagating the error.
                error = exc
            if error is None:
                token = response.get("token")
                if not isinstance(token, str) or not token:
                    error = AdapterError("AUTH_ERROR", "Hotelbook: токен не получен", category="auth")
                else:
                    cache.set(key, encrypt_value(token), 86340)
        if error:
            raise error
        return token

    def _request(self, ctx, operation, method, path, payload=None):
        credential, config = self._config(ctx)
        token = self._token(ctx, credential, config)
        try:
            return self._wire(
                ctx, operation, method, path, payload, token, (config["login"], config["password"])
            )
        except AdapterError as exc:
            if exc.code != "AUTH_ERROR":
                raise
            with transaction.atomic():
                SupplierCredential.objects.select_for_update().filter(pk=credential.pk).first()
                key = self._cache_key(credential, config, "token")
                cached = cache.get(key)
                # Another worker may already have replaced the rejected JWT.
                if cached and decrypt_value(cached) == token:
                    cache.delete(key)
            token = self._token(ctx, credential, config)
            # A rejected JWT has not performed the requested operation; retry once.
            return self._wire(
                ctx, operation, method, path, payload, token, (config["login"], config["password"])
            )

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
                cache.set(key, rows, self.dictionary_ttl)
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
            cache.set(key, data, self.dictionary_ttl)
        return data

    def _search_payload(self, ctx, credential, config, criteria):
        try:
            if not isinstance(criteria, dict):
                raise ValueError
            check_in = date.fromisoformat(criteria["check_in"])
            check_out = date.fromisoformat(criteria["check_out"])
            if check_out <= check_in:
                raise ValueError
            rooms = criteria.get("rooms", 1)
            if isinstance(rooms, int):
                guests = int(criteria.get("guests", 2))
                if isinstance(rooms, bool) or not 1 <= rooms <= 5 or guests < rooms:
                    raise ValueError
                rooms = [
                    {"adults": guests // rooms + (i < guests % rooms), "quantity": 1} for i in range(rooms)
                ]
            if (
                not isinstance(rooms, list)
                or not rooms
                or any(not isinstance(r, dict) or not isinstance(r.get("quantity", 1), int) for r in rooms)
                or sum(r.get("quantity", 1) for r in rooms) > 5
            ):
                raise ValueError
            for room in rooms:
                if not isinstance(room, dict):
                    raise ValueError
                if (
                    not isinstance(room["adults"], int)
                    or isinstance(room["adults"], bool)
                    or not isinstance(room.get("quantity", 1), int)
                    or not 1 <= room["adults"] <= 9
                    or not 1 <= room.get("quantity", 1) <= 5
                ):
                    raise ValueError
                if (
                    not isinstance(room.get("childrenAges", []), list)
                    or len(room.get("childrenAges", [])) > 4
                    or any(not 0 <= a <= 17 for a in room.get("childrenAges", []))
                ):
                    raise ValueError
        except (KeyError, ValueError, TypeError):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Проверьте даты и состав номеров Hotelbook", category="validation"
            ) from None
        hb = criteria.get("hotelbook") or {}
        if not isinstance(hb, dict) or (
            "confirmationMode" in hb and hb["confirmationMode"] not in ("ONLINE", "ON_REQUEST")
        ):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Некорректные параметры Hotelbook", category="validation"
            )
        hotels = hb.get("hotels")
        if hotels is not None and (not isinstance(hotels, list) or not hotels):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "hotels должен быть непустым списком ID", category="validation"
            )
        location = str(criteria.get("location", "")).strip()
        if not hotels and location.isdigit():
            hotels = [int(location)]
        if not hotels:
            normalized_location = " ".join(location.casefold().replace("ё", "е").replace("-", " ").split())
            hotels = [
                hotel_id
                for hotel_id, name in TEST_HOTELS.items()
                if location == str(hotel_id)
                or normalized_location
                == " ".join(name.casefold().replace("ё", "е").replace("-", " ").split())
                or (
                    credential.environment in ("sandbox", "test")
                    and len(normalized_location) >= 3
                    and not normalized_location.isdigit()
                    and normalized_location
                    in " ".join(name.casefold().replace("ё", "е").replace("-", " ").split())
                )
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
                    "TEST_HOTEL_REQUIRED", "Доступны только тестовые отели Hotelbook", category="validation"
                )
            if not 1 <= len(hotels) <= 500:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Hotelbook: от 1 до 500 отелей", category="validation"
                )
            payload["hotels"] = hotels
        elif hb.get("city_id"):
            try:
                city_id = int(hb["city_id"])
                if city_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Некорректный Hotelbook city ID", category="validation"
                ) from None
            if sandbox:
                payload["hotels"] = [
                    h for h in TEST_HOTELS if self._hotel(ctx, config, h).get("cityId") == city_id
                ]
                if not payload["hotels"]:
                    raise AdapterError(
                        "TEST_HOTEL_REQUIRED", "В городе нет тестовых отелей", category="availability"
                    )
            else:
                payload["cities"] = [city_id]
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
        try:
            payload["citizenshipId"] = int(citizenship)
            if payload["citizenshipId"] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Некорректный Hotelbook citizenship ID", category="validation"
            ) from None
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
        citizenship_code = criteria.get("citizenship") or config.get("default_citizenship", "")
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
                if (
                    not isinstance(offer, dict)
                    or not isinstance(offer.get("offerId"), str)
                    or not isinstance(offer.get("hotelId"), int)
                ):
                    raise AdapterError("INVALID_RESPONSE", "Hotelbook: некорректный результат поиска")
                if offer["offerId"] in seen:
                    continue
                seen.add(offer["offerId"])
                hotel = self._hotel(ctx, config, offer["hotelId"])
                snapshot = self._normalize(offer, search_id, expires_at, hotel, payload)
                if citizenship_code and (criteria.get("hotelbook") or {}).get("citizenship_id") is None:
                    snapshot["hotelbook"]["citizenship_ids"] = {
                        str(citizenship_code).upper(): payload["citizenshipId"]
                    }
                yield snapshot
            # finished describes supplier computation, not pagination exhaustion.
            if result["finished"] and offset >= result["totalOffers"]:
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

    @staticmethod
    def _normalized_fines(rows):
        # Search Fine.price and details Fine.amount represent the same money object.
        return [
            {
                **{k: v for k, v in fine.items() if k != "amount"},
                "price": fine.get("price") or fine.get("amount") or {},
            }
            for fine in (rows or [])
        ]

    def _rules(self, offer):
        policies = offer.get("finePolicies") or offer.get("fines") or {}
        cancel = self._normalized_fines(policies.get("cancel"))
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
            "change": self._normalized_fines(policies.get("change")),
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
        try:
            return self._normalize_offer(offer, search_id, expires_at, hotel, search_request)
        except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
            raise AdapterError(
                "INVALID_RESPONSE", "Hotelbook: некорректные данные предложения", category="internal"
            ) from None

    def _normalize_offer(self, offer, search_id, expires_at, hotel, search_request):
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
                **(
                    {"check_in_time": details["earliestCheckInTime"]}
                    if details.get("earliestCheckInTime")
                    else {}
                ),
                **(
                    {"check_out_time": details["latestCheckOutTime"]}
                    if details.get("latestCheckOutTime")
                    else {}
                ),
            },
            "fare": {
                "name": offer.get("roomGroupName") or "Тариф поставщика",
                **rules,
                "confirmation_mode": offer.get("confirmationMode"),
                "rooms": [
                    {
                        k: r[k]
                        for k in (
                            "hash",
                            "quantity",
                            "adults",
                            "children",
                            "roomName",
                            "beds",
                            "information",
                            "providerRoomId",
                            "roomInfoId",
                        )
                        if k in r
                    }
                    for r in rooms
                ],
                **{k: offer[k] for k in ("roomDescription", "isRemarkFreeFormAllowed") if k in offer},
            },
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
        policies = deepcopy(offer.get("finePolicies") or offer.get("fines") or {})
        for kind in ("cancel", "change"):
            policies[kind] = self._normalized_fines(policies.get(kind))
            for fine in policies[kind]:
                money = fine["price"]
                if "amount" in money:
                    # Decimal comparison also ignores JSON number/string formatting.
                    fine["price"] = {**money, "amount": Decimal(str(money["amount"]))}
        return {
            "confirmation": offer.get("confirmationMode"),
            "fines": policies,
            "rooms": [
                {k: room.get(k) for k in ("hash", "quantity", "adults", "children")}
                for room in offer.get("rooms", [])
            ],
        }

    def revalidate(self, ctx, offer_snapshot):
        if offer_snapshot.get("certification_run_id"):
            ctx.certification_run_id = offer_snapshot["certification_run_id"]
        hb = offer_snapshot.get("hotelbook") or {}
        if not hb.get("search_id") or not hb.get("offer_id"):
            raise AdapterError(
                "RATE_UNAVAILABLE", "Нет идентификаторов предложения Hotelbook", category="availability"
            )
        expires = parse_datetime(offer_snapshot.get("expires_at", ""))
        if expires and expires <= timezone.now():
            raise AdapterError(
                "OFFER_EXPIRED", "Поиск Hotelbook устарел; выполните новый поиск", category="availability"
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
        for key in ("special_requests", "main_guest"):
            if key in offer_snapshot:
                normalized[key] = offer_snapshot[key]
        if ctx.certification_run_id:
            normalized["certification_run_id"] = str(ctx.certification_run_id)
        if hb.get("citizenship_ids"):
            normalized["hotelbook"]["citizenship_ids"] = deepcopy(hb["citizenship_ids"])
        normalized["hotelbook_revalidation"] = signing.dumps(
            self._validation_identity(ctx, normalized), salt="hbpro.revalidation"
        )
        changed = normalized["price"] != offer_snapshot.get("price")
        return {
            "status": "price_changed" if changed else "valid",
            "terms_changed": self._terms(offer) != self._terms(hb.get("offer", {})),
            "price": normalized["price"],
            "snapshot": normalized,
            "fare_rules": self._rules(offer),
        }

    def _validation_identity(self, ctx, snapshot):
        # Bind the proof to this supplier, credentials, offer, price and search inputs.
        credential, config = self._config(ctx)
        data = {k: snapshot.get(k) for k in ("hotelbook", "price", "expires_at", "external_key")}
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return {"credential": self._cache_key(credential, config, "validation"), "digest": digest}

    def _booking_validation(self, ctx, snapshot):
        proof = snapshot.get("hotelbook_revalidation")
        if proof:
            try:
                identity = signing.loads(proof, salt="hbpro.revalidation", max_age=self.revalidation_ttl)
                expires = parse_datetime(snapshot.get("expires_at", ""))
                if (
                    expires
                    and timezone.is_aware(expires)
                    and expires > timezone.now()
                    and identity == self._validation_identity(ctx, snapshot)
                ):
                    return {"status": "valid", "snapshot": deepcopy(snapshot)}
            except (signing.BadSignature, ValueError, TypeError):
                pass
        return self.revalidate(ctx, snapshot)

    def fare_rules(self, ctx, offer_snapshot):
        return self.revalidate(ctx, offer_snapshot)["fare_rules"]

    def _guests(self, ctx, config, snapshot, passengers):
        offer = snapshot["hotelbook"]["offer"]
        check_in = date.fromisoformat(offer["checkIn"])
        prepared = []
        citizenship_ids = dict(snapshot["hotelbook"].get("citizenship_ids") or {})
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
                if not isinstance(first, str) or not isinstance(last, str):
                    raise ValueError
                first, last = first.strip(), last.strip()
                if not first or not last or len(first) > 50 or len(last) > 50 or age < 0:
                    raise ValueError
                if not offer.get("isCyrillicGuestNameAllowed") and re.search(r"[А-Яа-яЁё]", first + last):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise AdapterError(
                    "GUEST_DATA_MISSING", "Проверьте ФИО, пол и даты рождения гостей", category="validation"
                ) from None
            code = str(person.get("citizenship") or "").upper()
            if code not in citizenship_ids:
                citizenship_ids[code] = self._country_id(ctx, config, code)
            guest = {
                "firstName": first,
                "lastName": last,
                "gender": gender,
                "citizenship": citizenship_ids[code],
                "isChild": age < 18,
            }
            if age < 18:
                guest["age"] = age
            prepared.append((person.get("room_ref", ""), guest))
        result = []
        room_index = 0
        for room in offer["rooms"]:
            for _ in range(room.get("quantity", 1)):
                guests = []
                for is_child, ages in ((False, [None] * room["adults"]), (True, room.get("children") or [])):
                    for age in ages:
                        index = next(
                            (
                                i
                                for i, (ref, g) in enumerate(prepared)
                                if (not ref or ref == room["hash"] or ref == f"room:{room_index}")
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
                room_index += 1
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
        ctx.certification_run_id = snapshot.get("certification_run_id")
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
        ctx.order_id = service.order_id
        validation = self._booking_validation(ctx, snapshot)
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
        contact = booking_request.get("contact_info") or config.get("contact_info") or {}
        if not isinstance(contact, dict) or not str(contact.get("name") or "").strip():
            raise AdapterError("CONTACT_REQUIRED", "Заполните имя контактного лица", category="validation")
        remark = booking_request.get("special_requests") or snapshot.get("special_requests") or ""
        if remark and (
            not new_offer.get("isRemarkFreeFormAllowed") or not isinstance(remark, str) or len(remark) > 2000
        ):
            raise AdapterError(
                "INVALID_REQUEST",
                "Пожелания недоступны для тарифа или превышают 2000 символов",
                category="validation",
            )
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
            booking.update(order_id=order["orderId"], state="booking", created_at=timezone.now().isoformat())
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
            if remark:
                payload["freeRemark"] = remark
            booking["room_requests"] = [
                {"room_index": index, "hash": r["hash"]} for index, r in enumerate(rooms)
            ]
            if "client3Did" in hb["search_request"]:
                payload["client3Did"] = hb["search_request"]["client3Did"]
            response = self._request(ctx, "book", "POST", self._path(config, "hotel/gateway/book"), payload)
            items = response.get("items") or []
            if not items or not all(i.get("itemId") for i in items):
                raise AdapterError(
                    "BOOKING_UNKNOWN", "Hotelbook: части брони не получены", category="booking_unknown"
                )
            booking.update(items=items, state="complete")
            result = self._booking_result(locator, items, updated)
            self._save_booking_snapshot(ctx, service_id, updated)
            return result
        except AdapterError as exc:
            booking["state"] = "unknown" if exc.category == "booking_unknown" else "failed"
            booking["normalized_status"] = "unknown" if exc.category == "booking_unknown" else "failed"
            booking["error"] = {"code": exc.code, "message": str(exc)}
            self._save_booking_snapshot(ctx, service_id, updated)
            raise

    def _booking_result(self, locator, items, snapshot=None):
        normalized = normalized_status(items)
        status = {"confirmed": "booked", "cancelled": "cancelled"}.get(normalized, "pending")
        supplier_statuses = [i.get("status", i.get("state", "UNKNOWN")) for i in items]
        result = {
            "locator": locator,
            "status": status,
            "normalized_status": normalized,
            "supplier_statuses": supplier_statuses,
        }
        if snapshot is not None:
            booking = snapshot.get("hotelbook_booking", {})
            booking.update(
                normalized_status=normalized,
                supplier_statuses=supplier_statuses,
                updated_at=timezone.now().isoformat(),
            )
            booking["room_items"] = [
                {
                    "item_id": i.get("itemId"),
                    "rooms": sanitize(i.get("rooms", [])),
                    "provider_reference_id": (i.get("providerData") or {}).get("providerReferenceId"),
                    "supplier_status": i.get("status", i.get("state")),
                }
                for i in items
            ]
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
        service, _, _ = self._stored_booking(ctx, locator)
        with booking_lease(service):
            return self._retrieve_locked(ctx, locator)

    def _retrieve_locked(self, ctx, locator):
        service, snapshot, booking = self._stored_booking(ctx, locator)
        ctx.order_id = service.order_id
        ctx.certification_run_id = snapshot.get("certification_run_id")
        _, config = self._config(ctx)
        if not booking.get("order_id"):
            orders = self._request(
                ctx,
                "search_orders",
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
        ids = [i.get("itemId") for i in booking.get("items", [])]
        if not ids or not all(ids):
            order = self._request(
                ctx, "order_info", "GET", self._path(config, f"order/gateway/orders/{booking['order_id']}")
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
        cancelling = booking.get("state") == "cancelling"
        booking.update(
            items=items,
            state="cancelling" if cancelling and normalized_status(items) != "cancelled" else "complete",
        )
        result = self._booking_result(locator, items, snapshot)
        self._save_booking_snapshot(ctx, service.id, snapshot)
        return result

    def cancel(self, ctx, locator):
        service, _, _ = self._stored_booking(ctx, locator)
        with booking_lease(service):
            return self._cancel_locked(ctx, locator)

    def _cancel_locked(self, ctx, locator):
        self._retrieve_locked(ctx, locator)
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
