from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.views import APIView


class SessionAwareJWTScheme(OpenApiAuthenticationExtension):
    target_class = "accounts.authentication.SessionAwareJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):  # noqa: ARG002
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Access JWT. В web-клиенте токен хранится в HttpOnly cookie BFF.",
        }


class FallbackSerializer(serializers.Serializer):
    """Консервативная схема для командных APIView без declarative serializer.

    Endpoint остаётся видимым в OpenAPI. Точные схемы постепенно задаются через
    extend_schema, но отсутствие аннотации больше не удаляет операцию из документации.
    """


class SafeAutoSchema(AutoSchema):
    def get_operation_id(self) -> str:
        operation_id = super().get_operation_id()
        if self.method == "GET" and "List" in self.view.__class__.__name__:
            suffix = "_retrieve"
            if operation_id.endswith(suffix):
                return f"{operation_id.removesuffix(suffix)}_list"
        return operation_id

    def _get_serializer(self):
        view = self.view
        if isinstance(view, GenericAPIView):
            try:
                view.get_serializer_class()
            except (AssertionError, AttributeError, TypeError):
                return FallbackSerializer(context=self._serializer_context(view))
        elif isinstance(view, APIView) and not any(
            hasattr(view, attribute)
            for attribute in ("serializer_class", "get_serializer", "get_serializer_class")
        ):
            return FallbackSerializer(context=self._serializer_context(view))
        return super()._get_serializer()

    @staticmethod
    def _serializer_context(view):
        return {"request": getattr(view, "request", None), "view": view}
