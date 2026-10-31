"""Shared safe HTTP/state machinery for documented JSON supplier adapters."""

import hashlib
import hmac
import json
import re
import socket
import time
from copy import deepcopy
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.utils import timezone

from integrations.adapters import AdapterError, ProviderAdapter
from suppliers.models import SupplierCredential


class NoSupplierRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonProviderAdapter(ProviderAdapter):
    persists_booking_snapshot = True
    requires_revalidation_before_booking = True
    http_timeout = 45
    max_response_bytes = 8 * 1024 * 1024
    required_config = ()
    default_url = ""

    def _config(self, ctx, credential=None):
        if credential is None:
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
                .order_by("id")
                .first()
            )
        if (
            credential is None
            or credential.provider_adapter != self.key
            or str(credential.tenant_id) != str(ctx.tenant_id)
            or str(credential.supplier_id) != str(ctx.supplier_id)
        ):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "Нет настроенного доступа выбранного поставщика",
                category="configuration",
            )
        try:
            config = json.loads(credential.encrypted_secrets)
            if not isinstance(config, dict):
                raise ValueError
        except (TypeError, ValueError, RuntimeError):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "Некорректная конфигурация поставщика", category="configuration"
            ) from None
        config = {**config, "url": str(config.get("url") or self.default_url).rstrip("/")}
        if any(not config.get(field) for field in self.required_config):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "Не заполнены обязательные API-реквизиты: " + ", ".join(self.required_config),
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
                "Нужен HTTPS URL API без credentials и query",
                category="configuration",
            )
        if credential.environment != self.configured_environment():
            raise AdapterError(
                "ENVIRONMENT_MISMATCH",
                "Окружение доступа не совпадает с окружением адаптера",
                category="configuration",
            )
        return credential, config

    def _http(self, ctx, config, operation, method, path, payload=None, *, headers=None, mutation=False):
        started = time.monotonic()
        status = None
        error = None
        response = None
        request = Request(
            config["url"] + path,
            data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with build_opener(NoSupplierRedirect()).open(request, timeout=self.http_timeout) as result:
                status = result.status
                raw = result.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ValueError
                response = json.loads(raw)
                if not isinstance(response, (dict, list)):
                    raise ValueError
        except HTTPError as exc:
            status = exc.code
            provider_code = ""
            try:
                error_payload = json.loads(exc.read(self.max_response_bytes + 1))
                details = error_payload.get("error") or {} if isinstance(error_payload, dict) else {}
                candidate = str(details.get("code", "")) if isinstance(details, dict) else ""
                secret_values = [
                    str(value)
                    for key, value in config.items()
                    if any(word in key.lower() for word in ("password", "secret", "token", "api_key"))
                ]
                secret_values += [
                    str(value).removeprefix("Bearer ").removeprefix("Basic ")
                    for value in (headers or {}).values()
                ]
                if re.fullmatch(r"[A-Z0-9_#.-]{1,60}", candidate) and candidate not in secret_values:
                    provider_code = candidate
            except (ValueError, UnicodeError, OSError, AttributeError):
                pass
            category = (
                "booking_unknown"
                if mutation and status >= 500 or mutation and 300 <= status < 400
                else "auth"
                if status in (401, 403)
                else "validation"
                if status in (400, 404, 422)
                else "internal"
            )
            error = AdapterError(
                "SUPPLIER_RESULT_UNKNOWN"
                if category == "booking_unknown"
                else "AUTH_ERROR"
                if category == "auth"
                else "SUPPLIER_HTTP_ERROR",
                f"{self.key}: HTTP {status}" + (f" (код {provider_code})" if provider_code else ""),
                category=category,
            )
            error.http_status = status
            error.provider_code = provider_code
        except (TimeoutError, socket.timeout, URLError, OSError):
            error = AdapterError(
                "SUPPLIER_RESULT_UNKNOWN" if mutation else "SUPPLIER_UNAVAILABLE",
                "Нет ответа от поставщика; проверьте статус операции"
                if mutation
                else "Поставщик временно недоступен",
                category="booking_unknown" if mutation else "timeout",
                retry_safe=not mutation,
            )
        except (ValueError, UnicodeError):
            error = AdapterError(
                "SUPPLIER_RESULT_UNKNOWN" if mutation else "INVALID_RESPONSE",
                "Поставщик вернул некорректный ответ",
                category="booking_unknown" if mutation else "internal",
            )
        # Never log raw payloads, bearer tokens, names, passports or provider echoes.
        self._log(
            ctx,
            operation,
            {"method": method, "request_fields": sorted(payload) if isinstance(payload, dict) else []},
            {"received": response is not None, "supplier_error_code": getattr(error, "provider_code", "")},
            result="error" if error else "success",
            error_code=error.code if error else "",
            duration_ms=int((time.monotonic() - started) * 1000),
            http_status=status,
        )
        if error:
            raise error
        return response

    def _proof(self, ctx, snapshot):
        safe = {k: v for k, v in snapshot.items() if k not in ("validation", self.key + "_booking")}
        data = json.dumps(
            {
                "tenant": str(ctx.tenant_id),
                "supplier": str(ctx.supplier_id),
                "offer": safe,
                "until": (snapshot.get("validation") or {}).get("until"),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hmac.new(settings.SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()

    def validated(self, ctx, snapshot):
        result = deepcopy(snapshot)
        result["validation"] = {"until": (timezone.now() + timedelta(seconds=30)).isoformat()}
        result["validation"]["proof"] = self._proof(ctx, result)
        return result

    def assert_snapshot(self, ctx, snapshot):
        identity = snapshot.get("provider_context") or {}
        if (
            snapshot.get("provider_adapter") != self.key
            or identity.get("supplier_id") != str(ctx.supplier_id)
            or identity.get("environment") != self.configured_environment()
        ):
            raise AdapterError(
                "AVAILABILITY_CONFLICT", "Оффер другого поставщика или окружения", category="validation"
            )

    def booking_snapshot(self, ctx, snapshot):
        self.assert_snapshot(ctx, snapshot)
        from django.utils.dateparse import parse_datetime

        proof = snapshot.get("validation") or {}
        until = parse_datetime(proof.get("until", ""))
        if (
            until
            and timezone.is_aware(until)
            and until > timezone.now()
            and hmac.compare_digest(str(proof.get("proof", "")), self._proof(ctx, snapshot))
        ):
            return deepcopy(snapshot)
        result = self.revalidate(ctx, snapshot)
        if result.get("price_changed") or result.get("terms_changed"):
            raise AdapterError(
                "RATE_CHANGED",
                "Условия изменились. Повторите preflight и подтвердите изменения.",
                category="price",
            )
        return deepcopy(result["snapshot"])

    def _service(self, ctx, service_id):
        from services.models import OrderService

        service = OrderService.objects.filter(
            pk=service_id,
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            source="api",
            archived_at__isnull=True,
        ).first()
        if service is None or (service.provider_snapshot or {}).get("provider_adapter") != self.key:
            raise AdapterError(
                "BOOKING_NOT_FOUND", "Услуга не принадлежит выбранному поставщику", category="validation"
            )
        return service

    def _stored(self, ctx, locator):
        from services.models import OrderService

        rows = list(
            OrderService.objects.filter(
                tenant_id=ctx.tenant_id,
                supplier_id=ctx.supplier_id,
                source="api",
                **{f"provider_snapshot__{self.key}_booking__locator": locator},
            )[:2]
        )
        if len(rows) != 1:
            raise AdapterError(
                "BOOKING_NOT_FOUND",
                "Бронь не найдена однозначно у выбранного поставщика",
                category="validation",
            )
        service = self._service(ctx, rows[0].pk)
        snapshot = deepcopy(service.provider_snapshot)
        return service, snapshot, snapshot[self.key + "_booking"]

    def _save(self, service, snapshot):
        from services.models import OrderService

        OrderService.objects.filter(
            pk=service.pk, tenant_id=service.tenant_id, supplier_id=service.supplier_id
        ).update(provider_snapshot=snapshot)
        service.provider_snapshot = deepcopy(snapshot)

    def reserve(self, service, snapshot):
        booking = snapshot.get(self.key + "_booking")
        if booking:
            raise AdapterError(
                "BOOKING_UNKNOWN",
                "Бронь уже отправлялась. Используйте status inquiry; повторное создание запрещено.",
                category="booking_unknown",
            )
        booking = {"locator": uuid4().hex, "state": "creating", "normalized_status": "pending"}
        snapshot[self.key + "_booking"] = booking
        self._save(service, snapshot)
        return booking

    def send_once(self, ctx, service, snapshot, marker, operation):
        booking = snapshot[self.key + "_booking"]
        if booking.get(marker):
            raise AdapterError(
                "OPERATION_UNKNOWN",
                "Операция уже отправлялась. Требуется status inquiry.",
                category="booking_unknown",
            )
        booking[marker] = True
        self._save(service, snapshot)
        try:
            return operation()
        except AdapterError as exc:
            booking["state"] = "unknown" if exc.category == "booking_unknown" else "failed"
            booking["last_error"] = exc.code
            self._save(service, snapshot)
            raise

    def booking_needs_sync(self, booking):
        terminal = {"cancelled", "refunded", "failed"}
        pending_mutation = bool(
            booking
            and booking.get("state") != "failed"
            and booking.get("normalized_status") not in terminal
            and (
                booking.get("cancel_sent")
                or booking.get("issue_sent")
                and booking.get("normalized_status") != "issued"
                or booking.get("approve_sent")
                and not booking.get("payment_confirmed")
            )
        )
        return bool(
            booking
            and booking.get("locator")
            and (
                booking.get("normalized_status") in ("pending", "unknown")
                or booking.get("state") in ("creating", "unknown", "cancelling")
                or pending_mutation
            )
        )

    def fare_rules(self, ctx, offer_snapshot):
        return deepcopy(offer_snapshot.get("fare") or {})

    def refund_quote(self, ctx, request):
        raise AdapterError(
            "NOT_SUPPORTED",
            "Отдельный автоматический возврат не подтверждён контрактом поставщика",
            category="configuration",
        )

    def issue(self, ctx, issue_request):
        raise AdapterError(
            "NOT_SUPPORTED",
            "Для этого поставщика отдельная выписка не поддерживается",
            category="configuration",
        )
