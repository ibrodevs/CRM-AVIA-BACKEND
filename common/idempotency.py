"""Идемпотентность команд (ТЗ §3.4).

Использование в командном endpoint-е:

    @idempotent_command("orders.transition")
    def post(self, request, ...): ...

Правила:
- заголовок Idempotency-Key обязателен для операций, где required=True;
- один ключ (tenant + user + endpoint) возвращает первоначальный ответ;
- повтор с другим телом — 409 IDEMPOTENCY_CONFLICT;
- параллельный повтор during in-progress — 409 с Retry-After.
"""
import functools
import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.response import Response

from common.errors import ApiError, IdempotencyConflictError
from common.models import IdempotencyRecord


def _request_hash(request) -> str:
    body = request.body or b""
    return hashlib.sha256(body).hexdigest()


def idempotent_command(endpoint_name: str, *, required: bool = True):
    def decorator(view_method):
        @functools.wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            key = request.headers.get("Idempotency-Key", "").strip()
            if not key:
                if required:
                    raise ApiError(
                        code="IDEMPOTENCY_KEY_REQUIRED",
                        message="Заголовок Idempotency-Key обязателен для этой операции",
                        status_code=400,
                    )
                return view_method(self, request, *args, **kwargs)
            if len(key) > 255:
                raise ApiError(code="IDEMPOTENCY_KEY_INVALID", message="Слишком длинный Idempotency-Key",
                               status_code=400)

            req_hash = _request_hash(request)
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        tenant_id=request.user.tenant_id,
                        user=request.user,
                        endpoint=endpoint_name,
                        key=key,
                        request_hash=req_hash,
                    )
            except IntegrityError:
                existing = IdempotencyRecord.objects.get(
                    tenant_id=request.user.tenant_id,
                    user=request.user,
                    endpoint=endpoint_name,
                    key=key,
                )
                if existing.request_hash != req_hash:
                    raise IdempotencyConflictError() from None
                if existing.status == "in_progress":
                    response = Response(
                        {"error": {"code": "IDEMPOTENT_REQUEST_IN_PROGRESS",
                                   "message": "Операция ещё выполняется", "fields": {}, "details": {},
                                   "request_id": getattr(request, "request_id", None)}},
                        status=409,
                    )
                    response["Retry-After"] = "2"
                    return response
                return Response(existing.response_body, status=existing.response_status)

            try:
                response = view_method(self, request, *args, **kwargs)
            except Exception:
                # Ошибка не резервирует ключ: клиент может повторить с тем же ключом.
                IdempotencyRecord.objects.filter(pk=record.pk).delete()
                raise

            body = None
            if hasattr(response, "data") and response.data is not None:
                from rest_framework.utils.encoders import JSONEncoder

                body = json.loads(json.dumps(response.data, cls=JSONEncoder))
            IdempotencyRecord.objects.filter(pk=record.pk).update(
                status="completed",
                response_status=response.status_code,
                response_body=body,
                completed_at=timezone.now(),
            )
            return response

        return wrapper

    return decorator
