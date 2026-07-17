import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import set_rollback

logger = logging.getLogger("travelhub.errors")


class ApiError(exceptions.APIException):
    """Базовая бизнес-ошибка с машинным кодом."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "BAD_REQUEST"

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        fields: dict | None = None,
        details: dict | None = None,
        status_code: int | None = None,
    ):
        self.code = code or self.default_code
        self.message = message or self.default_detail if hasattr(self, "default_detail") else (message or "")
        self.fields = fields or {}
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail=self.message, code=self.code)


class ValidationApiError(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_detail = "Некорректные данные запроса"


class ConflictError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "CONFLICT"
    default_detail = "Конфликт состояния ресурса"


class VersionConflictError(ConflictError):
    default_code = "VERSION_CONFLICT"
    default_detail = "Ресурс изменён другим пользователем"

    def __init__(self, current_version: int, allowed_actions: list[str] | None = None, **kwargs):
        details = kwargs.pop("details", {})
        details.setdefault("current_version", current_version)
        if allowed_actions is not None:
            details.setdefault("allowed_actions", allowed_actions)
        super().__init__(details=details, **kwargs)


class IdempotencyConflictError(ConflictError):
    default_code = "IDEMPOTENCY_CONFLICT"
    default_detail = "Idempotency-Key уже использован с другим телом запроса"


class TransitionForbiddenError(ConflictError):
    default_code = "STATUS_TRANSITION_FORBIDDEN"
    default_detail = "Недопустимый переход статуса"


class BusinessRejectionError(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "BUSINESS_RULE_VIOLATION"
    default_detail = "Операция отклонена бизнес-правилом"


class LockedError(ApiError):
    status_code = status.HTTP_423_LOCKED
    default_code = "RESOURCE_LOCKED"
    default_detail = "Ресурс заблокирован другой операцией"


class UpstreamError(ApiError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "UPSTREAM_FAILURE"
    default_detail = "Ошибка внешнего поставщика"


class EventCursorExpiredError(ApiError):
    status_code = status.HTTP_410_GONE
    default_code = "EVENT_CURSOR_EXPIRED"
    default_detail = "Cursor устарел; обновите открытые ресурсы и начните с нового cursor"


def _error_body(code: str, message: str, fields: dict | None, details: dict | None, request) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "fields": fields or {},
            "details": details or {},
            "request_id": getattr(request, "request_id", None) if request else None,
        }
    }


_DRF_CODE_MAP = {
    exceptions.AuthenticationFailed: "AUTHENTICATION_FAILED",
    exceptions.NotAuthenticated: "NOT_AUTHENTICATED",
    exceptions.PermissionDenied: "PERMISSION_DENIED",
    exceptions.NotFound: "NOT_FOUND",
    exceptions.MethodNotAllowed: "METHOD_NOT_ALLOWED",
    exceptions.Throttled: "THROTTLED",
    exceptions.UnsupportedMediaType: "UNSUPPORTED_MEDIA_TYPE",
    exceptions.ParseError: "PARSE_ERROR",
}


def api_exception_handler(exc, context):
    """DRF exception handler, приводящий все ошибки к контракту ТЗ §4.2."""
    request = context.get("request")

    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    if isinstance(exc, ApiError):
        set_rollback()
        return Response(
            _error_body(exc.code, exc.message, exc.fields, exc.details, request),
            status=exc.status_code,
        )

    if isinstance(exc, exceptions.ValidationError):
        set_rollback()
        fields = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        return Response(
            _error_body("VALIDATION_ERROR", "Некорректные данные запроса", fields, None, request),
            status=exc.status_code,
        )

    if isinstance(exc, exceptions.APIException):
        set_rollback()
        code = _DRF_CODE_MAP.get(type(exc), getattr(exc, "default_code", "ERROR")).upper()
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return Response(_error_body(code, message, None, None, request), status=exc.status_code)

    logger.exception("unhandled exception", extra={"request_id": getattr(request, "request_id", None)})
    set_rollback()
    return Response(
        _error_body("INTERNAL_ERROR", "Внутренняя ошибка сервера", None, None, request),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
