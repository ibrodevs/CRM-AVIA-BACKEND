import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from accounts.models import (
    DemoAccessRequest,
    PasswordResetToken,
    Role,
    User,
    UserPreference,
    UserRole,
    UserServiceAccess,
    UserSession,
)
from accounts.permissions import require
from accounts.serializers import (
    MeSerializer,
    RoleSerializer,
    UserCreateSerializer,
    UserPreferenceSerializer,
    UserSerializer,
    UserServiceAccessSerializer,
)
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from common.pagination import DefaultPagination


class DemoAccessRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = DemoAccessRequest
        fields = ["id", "name", "company", "email", "phone", "created_at"]
        read_only_fields = ["id", "created_at"]


class DemoAccessThrottle(AnonRateThrottle):
    scope = "public_response"


class DemoAccessRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [DemoAccessThrottle]

    def post(self, request):
        serializer = DemoAccessRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        serializer.save(
            source_ip=forwarded or request.META.get("REMOTE_ADDR") or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


def _get_user_or_404(request, user_id) -> User:
    user = User.objects.filter(pk=user_id, tenant_id=request.user.tenant_id).first()
    if user is None:
        raise ApiError(code="NOT_FOUND", message="Пользователь не найден", status_code=404)
    return user


class MeView(APIView):
    def get(self, request):
        return Response(MeSerializer(request.user).data)

    def patch(self, request):
        allowed = {
            "first_name",
            "last_name",
            "middle_name",
            "phone",
            "work_phone",
            "internal_phone",
            "telegram",
            "timezone",
            "language",
            "presence",
            "work_status",
        }
        data = {k: v for k, v in request.data.items() if k in allowed}
        serializer = MeSerializer(request.user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class MePreferencesView(APIView):
    def _get(self, user) -> UserPreference:
        pref, _ = UserPreference.objects.get_or_create(user=user)
        return pref

    def get(self, request):
        return Response(UserPreferenceSerializer(self._get(request.user)).data)

    def patch(self, request):
        serializer = UserPreferenceSerializer(self._get(request.user), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class MeAvatarView(APIView):
    def put(self, request):
        file = request.FILES.get("avatar")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл avatar обязателен", status_code=400)
        if file.size > 5 * 1024 * 1024:
            raise ApiError(code="FILE_TOO_LARGE", message="Максимальный размер аватара 5 МБ", status_code=400)
        if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise ApiError(code="UNSUPPORTED_FILE_TYPE", message="Допустимы JPEG/PNG/WebP", status_code=400)
        request.user.avatar = file
        request.user.save(update_fields=["avatar"])
        return Response({"avatar": request.user.avatar.url})

    def delete(self, request):
        request.user.avatar.delete(save=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserListCreateView(GenericAPIView):
    permission_classes = [require("users.manage")]
    pagination_class = DefaultPagination
    serializer_class = UserSerializer

    def get(self, request):
        qs = (
            User.objects.filter(tenant_id=request.user.tenant_id)
            .exclude(status=User.Status.ARCHIVED)
            .prefetch_related("user_roles__role")
            .order_by("last_name", "first_name")
        )
        q = request.query_params.get("q", "").strip()
        if q:
            from django.db.models import Q

            qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(UserSerializer(page, many=True).data)

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User(tenant_id=request.user.tenant_id, status=User.Status.INVITED, **serializer.validated_data)
        user.set_unusable_password()
        user.save()
        audit("users.created", request=request, resource=user)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class UserDetailView(APIView):
    permission_classes = [require("users.manage")]

    def get(self, request, user_id):
        return Response(UserSerializer(_get_user_or_404(request, user_id)).data)

    def patch(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        allowed = {
            "phone",
            "first_name",
            "last_name",
            "middle_name",
            "position",
            "department",
            "manager",
            "work_phone",
            "internal_phone",
            "telegram",
            "hired_at",
            "work_status",
            "timezone",
            "language",
            "sla_response_minutes",
        }
        data = {k: v for k, v in request.data.items() if k in allowed}
        serializer = UserSerializer(user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        before = {k: str(getattr(user, k)) for k in data}
        serializer.save()
        audit(
            "users.updated",
            request=request,
            resource=user,
            before=before,
            after={k: str(v) for k, v in data.items()},
        )
        return Response(serializer.data)


class UserInviteView(APIView):
    """Выдаёт приглашение (одноразовый токен установки пароля)."""

    permission_classes = [require("users.manage")]

    def post(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        if user.status not in (User.Status.INVITED, User.Status.SUSPENDED):
            raise ApiError(
                code="INVALID_USER_STATUS",
                message="Приглашение доступно только для invited/suspended",
                status_code=409,
            )
        raw_token = secrets.token_urlsafe(32)
        PasswordResetToken.objects.create(
            user=user,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        user.status = User.Status.ACTIVE if user.status == User.Status.SUSPENDED else user.status
        user.save(update_fields=["status"])
        emit_event("users.invited", user, tenant_id=user.tenant_id)
        audit("users.invited", request=request, resource=user)

        return Response({"invite_token": raw_token, "expires_in_days": 7})


class UserSuspendView(APIView):
    permission_classes = [require("users.manage")]

    def post(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        if user.pk == request.user.pk:
            raise ApiError(code="CANNOT_SUSPEND_SELF", message="Нельзя заблокировать себя", status_code=409)
        with transaction.atomic():
            user.status = User.Status.SUSPENDED
            user.save(update_fields=["status"])
            UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        audit("users.suspended", request=request, resource=user, reason=str(request.data.get("reason", "")))
        return Response(UserSerializer(user).data)


class UserRolesView(APIView):
    permission_classes = [require("roles.manage", "users.manage")]

    def get(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        roles = Role.objects.filter(user_roles__user=user).prefetch_related("permissions")
        return Response(RoleSerializer(roles, many=True).data)

    def put(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        codes = request.data.get("roles")
        if not isinstance(codes, list):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Ожидается список кодов ролей",
                fields={"roles": ["Обязательное поле-список"]},
                status_code=400,
            )
        roles = list(Role.objects.filter(tenant_id=request.user.tenant_id, code__in=codes))
        missing = set(codes) - {r.code for r in roles}
        if missing:
            raise ApiError(
                code="UNKNOWN_ROLE", message=f"Неизвестные роли: {sorted(missing)}", status_code=400
            )
        with transaction.atomic():
            before = list(user.user_roles.values_list("role__code", flat=True))
            user.user_roles.all().delete()
            UserRole.objects.bulk_create(
                [UserRole(user=user, role=role, assigned_by=request.user) for role in roles]
            )
        audit(
            "users.roles_changed",
            request=request,
            resource=user,
            before={"roles": before},
            after={"roles": codes},
        )
        return Response({"roles": codes})


class UserServiceAccessView(APIView):
    permission_classes = [require("users.manage")]

    def get(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        return Response(UserServiceAccessSerializer(user.service_access.all(), many=True).data)

    def put(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        serializer = UserServiceAccessSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user.service_access.all().delete()
            UserServiceAccess.objects.bulk_create(
                [UserServiceAccess(user=user, **item) for item in serializer.validated_data]
            )
        audit(
            "users.service_access_changed",
            request=request,
            resource=user,
            after={"access": serializer.validated_data},
        )
        return Response(serializer.validated_data)


class UserSlaView(APIView):
    permission_classes = [require("users.manage")]

    def put(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        minutes = request.data.get("sla_response_minutes")
        if minutes is not None and (not isinstance(minutes, int) or minutes <= 0):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Некорректный SLA",
                fields={"sla_response_minutes": ["Положительное число минут или null"]},
                status_code=400,
            )
        user.sla_response_minutes = minutes
        user.save(update_fields=["sla_response_minutes"])
        audit("users.sla_changed", request=request, resource=user, after={"sla_response_minutes": minutes})
        return Response({"sla_response_minutes": minutes})


class RoleListView(APIView):
    permission_classes = [require("roles.manage", "users.manage")]

    def get(self, request):
        roles = Role.objects.filter(tenant_id=request.user.tenant_id).prefetch_related("permissions")
        return Response(RoleSerializer(roles, many=True).data)
