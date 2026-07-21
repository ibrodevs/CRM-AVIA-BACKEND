import hashlib
import secrets
from datetime import timedelta

import pyotp
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import FailedLoginAttempt, PasswordResetToken, TwoFactorConfig, User, UserSession
from accounts.tokens import issue_2fa_challenge, issue_session_tokens, rotate_session_tokens
from common.audit import audit
from common.errors import ApiError

BRUTE_FORCE_WINDOW = timedelta(minutes=15)
BRUTE_FORCE_LIMIT = 10


class LoginThrottle(AnonRateThrottle):
    scope = "login"


class PasswordResetThrottle(AnonRateThrottle):
    scope = "password_reset"


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")


def _check_brute_force(identifiers: list[str]) -> None:
    since = timezone.now() - BRUTE_FORCE_WINDOW
    count = FailedLoginAttempt.objects.filter(identifier__in=identifiers, attempted_at__gte=since).count()
    if count >= BRUTE_FORCE_LIMIT:
        raise ApiError(
            code="TOO_MANY_LOGIN_ATTEMPTS",
            message="Слишком много неуспешных попыток входа. Повторите позже.",
            status_code=429,
        )


class LoginSerializer(serializers.Serializer):
    login = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        login = serializer.validated_data["login"].strip().lower()
        ip = _client_ip(request)
        _check_brute_force([login, ip])

        user = User.objects.filter(Q(email__iexact=login) | Q(phone=login)).first()
        authenticated = None
        if user is not None:
            authenticated = authenticate(
                request, username=user.email, password=serializer.validated_data["password"]
            )
        if authenticated is None or user.status != User.Status.ACTIVE:
            FailedLoginAttempt.objects.bulk_create(
                [
                    FailedLoginAttempt(identifier=login),
                    FailedLoginAttempt(identifier=ip),
                ]
            )
            audit(
                "auth.login_failed",
                request=request,
                reason=f"login={login}",
                tenant_id=user.tenant_id if user else None,
            )
            raise ApiError(code="INVALID_CREDENTIALS", message="Неверный логин или пароль", status_code=401)

        two_factor = getattr(user, "two_factor", None)
        if two_factor is not None and two_factor.is_enabled:
            audit("auth.login_2fa_challenge", actor=user, request=request, tenant_id=user.tenant_id)
            return Response({"two_factor_required": True, "challenge_token": issue_2fa_challenge(user)})

        tokens = issue_session_tokens(user, request)
        audit("auth.login", actor=user, request=request, tenant_id=user.tenant_id)
        return Response({"access": tokens["access"], "refresh": tokens["refresh"]})


class TwoFactorVerifySerializer(serializers.Serializer):
    challenge_token = serializers.CharField()
    code = serializers.CharField(max_length=10)


class TwoFactorVerifyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = TwoFactorVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token = JWTAuthentication().get_validated_token(
                serializer.validated_data["challenge_token"].encode()
            )
        except Exception:
            raise ApiError(
                code="INVALID_CHALLENGE", message="Недействительный challenge token", status_code=401
            ) from None
        if token.get("scope") != "2fa":
            raise ApiError(
                code="INVALID_CHALLENGE", message="Недействительный challenge token", status_code=401
            )

        user = User.objects.filter(pk=token["user_id"], status=User.Status.ACTIVE).first()
        two_factor = getattr(user, "two_factor", None) if user else None
        if user is None or two_factor is None or not two_factor.is_enabled:
            raise ApiError(code="INVALID_CHALLENGE", message="2FA не настроена", status_code=401)

        _check_brute_force([user.email, _client_ip(request)])
        totp = pyotp.TOTP(two_factor.totp_secret)
        if not totp.verify(serializer.validated_data["code"], valid_window=1):
            FailedLoginAttempt.objects.create(identifier=user.email)
            audit("auth.2fa_failed", actor=user, request=request, tenant_id=user.tenant_id)
            raise ApiError(code="INVALID_2FA_CODE", message="Неверный код подтверждения", status_code=401)

        tokens = issue_session_tokens(user, request)
        audit("auth.login", actor=user, request=request, reason="2fa", tenant_id=user.tenant_id)
        return Response({"access": tokens["access"], "refresh": tokens["refresh"]})


class TwoFactorStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        two_factor = getattr(request.user, "two_factor", None)
        return Response(
            {
                "enabled": bool(two_factor and two_factor.is_enabled),
                "confirmed_at": two_factor.confirmed_at if two_factor and two_factor.is_enabled else None,
            }
        )


class TwoFactorSetupView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = getattr(request.user, "two_factor", None)
        if current is not None and current.is_enabled:
            raise ApiError(code="TWO_FACTOR_ALREADY_ENABLED", message="2FA уже включена", status_code=409)
        secret = pyotp.random_base32()
        config, _ = TwoFactorConfig.objects.update_or_create(
            user=request.user,
            defaults={"totp_secret": secret, "confirmed_at": None},
        )
        audit("auth.2fa_setup_started", request=request, resource=config)
        return Response(
            {
                "secret": secret,
                "provisioning_uri": pyotp.TOTP(secret).provisioning_uri(
                    name=request.user.email,
                    issuer_name="Travel Hub CRM",
                ),
            }
        )


class TwoFactorConfirmSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=10)


class TwoFactorConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TwoFactorConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = getattr(request.user, "two_factor", None)
        if config is None:
            raise ApiError(code="TWO_FACTOR_NOT_STARTED", message="Сначала начните настройку 2FA", status_code=409)
        if not pyotp.TOTP(config.totp_secret).verify(serializer.validated_data["code"], valid_window=1):
            audit("auth.2fa_confirm_failed", request=request, resource=config)
            raise ApiError(code="INVALID_2FA_CODE", message="Неверный код подтверждения", status_code=400)
        config.confirmed_at = timezone.now()
        config.save(update_fields=["confirmed_at"])
        audit("auth.2fa_enabled", request=request, resource=config)
        return Response({"enabled": True, "confirmed_at": config.confirmed_at})


class TwoFactorDisableSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(max_length=10)


class TwoFactorDisableView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TwoFactorDisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            raise ApiError(code="INVALID_CURRENT_PASSWORD", message="Текущий пароль неверен", status_code=400)
        config = getattr(user, "two_factor", None)
        if config is None or not config.is_enabled:
            return Response({"enabled": False, "confirmed_at": None})
        if not pyotp.TOTP(config.totp_secret).verify(serializer.validated_data["code"], valid_window=1):
            audit("auth.2fa_disable_failed", request=request, resource=config)
            raise ApiError(code="INVALID_2FA_CODE", message="Неверный код подтверждения", status_code=400)
        config.delete()
        audit("auth.2fa_disabled", request=request)
        return Response({"enabled": False, "confirmed_at": None})


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class TokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            refresh = RefreshToken(serializer.validated_data["refresh"])
        except TokenError:
            raise ApiError(
                code="INVALID_REFRESH_TOKEN", message="Недействительный refresh token", status_code=401
            ) from None

        session = UserSession.objects.filter(refresh_jti=refresh["jti"]).select_related("user").first()
        if session is None or not session.is_active:
            raise ApiError(code="SESSION_REVOKED", message="Сессия завершена", status_code=401)
        user = session.user
        if user.status != User.Status.ACTIVE:
            raise ApiError(code="USER_INACTIVE", message="Учётная запись неактивна", status_code=401)

        tokens = rotate_session_tokens(user, session, refresh)
        return Response(tokens)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        sid = getattr(request.auth, "payload", request.auth or {}).get("sid") if request.auth else None
        if sid:
            session = UserSession.objects.filter(pk=sid, user=request.user).first()
            if session:
                session.revoke()
        audit("auth.logout", request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LogoutAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        sid = getattr(request.auth, "payload", request.auth or {}).get("sid") if request.auth else None
        sessions = UserSession.objects.filter(user=request.user, revoked_at__isnull=True)
        if sid:
            sessions = sessions.exclude(pk=sid)
        count = sessions.update(revoked_at=timezone.now())
        audit("auth.logout_all", request=request, reason=f"revoked={count}")
        return Response({"revoked_sessions": count})


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            raise ApiError(code="INVALID_CURRENT_PASSWORD", message="Текущий пароль неверен", status_code=400)
        try:
            validate_password(serializer.validated_data["new_password"], user=user)
        except DjangoValidationError as exc:
            raise ApiError(
                code="WEAK_PASSWORD",
                message="Пароль не соответствует требованиям",
                fields={"new_password": exc.messages},
                status_code=400,
            ) from None
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        sid = getattr(request.auth, "payload", request.auth or {}).get("sid") if request.auth else None
        sessions = UserSession.objects.filter(user=user, revoked_at__isnull=True)
        if sid:
            sessions = sessions.exclude(pk=sid)
        sessions.update(revoked_at=timezone.now())
        audit("auth.password_changed", request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        email = str(request.data.get("email", "")).strip().lower()
        user = User.objects.filter(email__iexact=email, status=User.Status.ACTIVE).first()
        if user is not None:
            raw_token = secrets.token_urlsafe(32)
            PasswordResetToken.objects.create(
                user=user,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                expires_at=timezone.now() + timedelta(hours=1),
            )

            from common.outbox import emit_event

            emit_event(
                "auth.password_reset_requested", user, payload={"email": user.email}, tenant_id=user.tenant_id
            )
            audit("auth.password_reset_requested", actor=user, request=request, tenant_id=user.tenant_id)

        return Response({"detail": "Если аккаунт существует, инструкция отправлена на email"})


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(trim_whitespace=False)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_hash = hashlib.sha256(serializer.validated_data["token"].encode()).hexdigest()
        record = (
            PasswordResetToken.objects.filter(
                token_hash=token_hash, used_at__isnull=True, expires_at__gt=timezone.now()
            )
            .select_related("user")
            .first()
        )
        if record is None:
            raise ApiError(
                code="INVALID_RESET_TOKEN", message="Токен недействителен или истёк", status_code=400
            )
        user = record.user
        try:
            validate_password(serializer.validated_data["new_password"], user=user)
        except DjangoValidationError as exc:
            raise ApiError(
                code="WEAK_PASSWORD",
                message="Пароль не соответствует требованиям",
                fields={"new_password": exc.messages},
                status_code=400,
            ) from None
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
        UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        audit("auth.password_reset_confirmed", actor=user, request=request, tenant_id=user.tenant_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserSession
        fields = ["id", "ip_address", "user_agent", "created_at", "last_seen_at", "expires_at"]


class SessionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        current_sid = (
            getattr(request.auth, "payload", request.auth or {}).get("sid") if request.auth else None
        )
        sessions = UserSession.objects.filter(
            user=request.user, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-last_seen_at")
        data = SessionSerializer(sessions, many=True).data
        for item in data:
            item["is_current"] = item["id"] == current_sid
        return Response({"count": len(data), "results": data})


class SessionDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, session_id):
        session = UserSession.objects.filter(pk=session_id, user=request.user).first()
        if session is None:
            raise ApiError(code="NOT_FOUND", message="Сессия не найдена", status_code=404)
        session.revoke()
        audit("auth.session_revoked", request=request, resource=session)
        return Response(status=status.HTTP_204_NO_CONTENT)
