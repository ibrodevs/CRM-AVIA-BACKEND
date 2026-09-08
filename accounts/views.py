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
    RolePermission,
    User,
    UserPreference,
    UserRole,
    UserServiceAccess,
    UserSession,
)
from accounts.permissions import require
from accounts.permissions_catalog import PERMISSIONS
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

    @transaction.atomic
    def patch(self, request):
        allowed = {
            "first_name",
            "last_name",
            "middle_name",
            "phone",
            "work_phone",
            "internal_phone",
            "telegram",
            "max",
            "whatsapp",
            "position",
            "department",
            "hired_at",
            "timezone",
            "language",
            "presence",
            "work_status",
        }
        data = {k: v for k, v in request.data.items() if k in allowed}
        serializer = MeSerializer(request.user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        if "language" in serializer.validated_data:
            preference, _ = UserPreference.objects.get_or_create(user=request.user)
            preference.language = request.user.language
            preference.save(update_fields=["language"])
        return Response(serializer.data)


class MePreferencesView(APIView):
    def _get(self, user) -> UserPreference:
        pref, _ = UserPreference.objects.get_or_create(user=user)
        return pref

    def get(self, request):
        return Response(UserPreferenceSerializer(self._get(request.user)).data)

    @transaction.atomic
    def patch(self, request):
        serializer = UserPreferenceSerializer(self._get(request.user), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        if "language" in serializer.validated_data:
            request.user.language = serializer.validated_data["language"]
            request.user.save(update_fields=["language"])
        return Response(serializer.data)


class MeAvatarView(APIView):
    def get(self, request):
        from django.http import FileResponse

        if not request.user.avatar:
            raise ApiError(code="NOT_FOUND", message="Аватар не загружен", status_code=404)
        try:
            response = FileResponse(request.user.avatar.open("rb"))
        except FileNotFoundError:
            raise ApiError(code="NOT_FOUND", message="Файл аватара не найден", status_code=404) from None
        response["Cache-Control"] = "private, no-store"
        return response

    def put(self, request):
        file = request.FILES.get("avatar")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл avatar обязателен", status_code=400)
        if file.size > 5 * 1024 * 1024:
            raise ApiError(code="FILE_TOO_LARGE", message="Максимальный размер аватара 5 МБ", status_code=400)
        if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise ApiError(code="UNSUPPORTED_FILE_TYPE", message="Допустимы JPEG/PNG/WebP", status_code=400)
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(file) as image:
                if image.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("unsupported image")
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise ApiError(code="INVALID_IMAGE", message="Файл не является допустимым изображением", status_code=400) from None
        file.seek(0)
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
        data = dict(serializer.validated_data)
        codes = data.pop("roles", [])
        roles = list(Role.objects.filter(tenant_id=request.user.tenant_id, code__in=codes))
        if set(codes) != {role.code for role in roles}:
            raise ApiError(code="UNKNOWN_ROLE", message="Неизвестная роль", status_code=400)
        password = data.pop("password", None)
        target_status = data.pop("status", User.Status.INVITED)
        user = User(tenant_id=request.user.tenant_id, status=target_status, **data)
        if target_status == User.Status.ACTIVE and not password:
            raise ApiError(code="VALIDATION_ERROR", message="Для активного пользователя задайте пароль", status_code=400)
        if password:
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError

            try:
                validate_password(password, user=user)
            except ValidationError as error:
                raise ApiError(code="WEAK_PASSWORD", message="; ".join(error.messages), status_code=400) from None
            user.set_password(password)
        else:
            user.set_unusable_password()
        with transaction.atomic():
            user.save()
            UserRole.objects.bulk_create([UserRole(user=user, role=role, assigned_by=request.user) for role in roles])
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
            "max",
            "whatsapp",
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


class UserActivateView(APIView):
    permission_classes = [require("users.manage")]

    def post(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        if user.status != User.Status.SUSPENDED:
            raise ApiError(code="INVALID_USER_STATUS", message="Пользователь не заблокирован", status_code=409)
        user.status = User.Status.ACTIVE if user.has_usable_password() else User.Status.INVITED
        user.save(update_fields=["status"])
        audit("users.activated", request=request, resource=user)
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
        kinds = [row["service_kind"] for row in serializer.validated_data]
        if len(kinds) != len(set(kinds)):
            raise ApiError(code="VALIDATION_ERROR", message="Виды услуг не должны повторяться", status_code=400)
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
    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method == "GET" and str(self.kwargs.get("user_id")) == str(request.user.pk):
            return
        if not require("users.manage")().has_permission(request, self):
            self.permission_denied(request)

    def get(self, request, user_id):
        user = _get_user_or_404(request, user_id)
        return Response({"sla_response_minutes": user.sla_response_minutes})

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

    def get(self, request):
        roles = Role.objects.filter(tenant_id=request.user.tenant_id).prefetch_related("permissions")
        return Response(RoleSerializer(roles, many=True).data)


def _get_role_or_404(request, role_id) -> Role:
    role = Role.objects.filter(pk=role_id, tenant_id=request.user.tenant_id).first()
    if role is None:
        raise ApiError(code="NOT_FOUND", message="Роль не найдена", status_code=404)
    return role


class RoleDetailView(APIView):
    permission_classes = [require("roles.manage")]

    def get(self, request, role_id):
        return Response(RoleSerializer(_get_role_or_404(request, role_id)).data)

    def put(self, request, role_id):
        role = _get_role_or_404(request, role_id)
        permissions = request.data.get("permissions")
        if not isinstance(permissions, list):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Ожидается список кодов прав",
                fields={"permissions": ["Обязательное поле-список"]},
                status_code=400,
            )
        unknown = sorted(set(permissions) - set(PERMISSIONS))
        if unknown:
            raise ApiError(
                code="UNKNOWN_PERMISSION",
                message=f"Неизвестные права: {unknown}",
                status_code=400,
            )
        if role.code == "admin" and set(permissions) != set(PERMISSIONS):
            raise ApiError(
                code="ADMIN_ROLE_IMMUTABLE",
                message="Системная роль admin всегда содержит полный набор прав",
                status_code=409,
            )
        before = sorted(role.permissions.values_list("permission_code", flat=True))
        wanted = set(permissions)
        with transaction.atomic():
            existing = set(role.permissions.values_list("permission_code", flat=True))
            RolePermission.objects.bulk_create(
                [RolePermission(role=role, permission_code=code) for code in sorted(wanted - existing)]
            )
            role.permissions.exclude(permission_code__in=wanted).delete()
        audit(
            "roles.permissions_changed",
            request=request,
            resource=role,
            before={"permissions": before},
            after={"permissions": sorted(wanted)},
        )
        role = Role.objects.prefetch_related("permissions").get(pk=role.pk)
        return Response(RoleSerializer(role).data)
