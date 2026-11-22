"""Server-side session lock, hashed PIN and activity-based expiry."""
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import FailedLoginAttempt, UserPreference, UserSession
from common.audit import audit
from common.errors import ApiError

ALLOWED_MINUTES = (0, 1, 5, 10, 15, 30, 60, 120, 240)


def enforce_session_security(user, token, path, method="GET"):
    sid = token.get('sid')
    if not sid:
        return
    session = UserSession.objects.filter(pk=sid, user=user).first()
    if session is None or not session.is_active:
        raise ApiError(code='SESSION_REVOKED', message='Сессия завершена', status_code=401)
    preference, _ = UserPreference.objects.get_or_create(user=user)
    elapsed = (timezone.now() - session.last_activity_at).total_seconds()
    if preference.auto_logout_minutes and elapsed >= preference.auto_logout_minutes * 60:
        session.revoke()
        raise ApiError(code='SESSION_EXPIRED', message='Время бездействия истекло. Войдите снова', status_code=401)
    if preference.pin_hash and preference.auto_lock_minutes and elapsed >= preference.auto_lock_minutes * 60:
        if not session.locked_at:
            UserSession.objects.filter(pk=session.pk).update(locked_at=timezone.now())
            session.locked_at = timezone.now()
    allowed = ('/api/v1/auth/security/', '/api/v1/auth/unlock/', '/api/v1/auth/logout/')
    if session.locked_at and path not in allowed and not (path == '/api/v1/me/' and method == 'GET'):
        raise ApiError(code='SESSION_LOCKED', message='Введите PIN, чтобы продолжить работу', status_code=423)


class SecuritySerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    pin = serializers.RegexField(r'^[0-9]{4}$', required=False, write_only=True)
    disable_pin = serializers.BooleanField(required=False, default=False)
    auto_lock_minutes = serializers.ChoiceField(choices=ALLOWED_MINUTES, required=False)
    auto_logout_minutes = serializers.ChoiceField(choices=ALLOWED_MINUTES, required=False)


def security_payload(pref, session=None):
    return {'pin_enabled': bool(pref.pin_hash), 'pin_length': pref.pin_length if pref.pin_hash else 4, 'auto_lock_minutes': pref.auto_lock_minutes,
            'auto_logout_minutes': pref.auto_logout_minutes, 'locked': bool(session and session.locked_at)}


class SecurityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pref, _ = UserPreference.objects.get_or_create(user=request.user)
        session = UserSession.objects.filter(pk=request.auth.get('sid'), user=request.user).first()
        return Response(security_payload(pref, session))

    def patch(self, request):
        data = SecuritySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        if not request.user.check_password(values.get('password', '')):
            raise ApiError(code='INVALID_PASSWORD', message='Укажите текущий пароль', status_code=400)
        with transaction.atomic():
            pref, _ = UserPreference.objects.select_for_update().get_or_create(user=request.user)
            if values.get('pin'):
                pref.pin_hash = make_password(values['pin'])
                pref.pin_length = 4
            if values.get('disable_pin'):
                pref.pin_hash = ''
                pref.auto_lock_minutes = 0
            for key in ['auto_lock_minutes', 'auto_logout_minutes']:
                if key in values:
                    setattr(pref, key, int(values[key]))
            if pref.auto_lock_minutes and not pref.pin_hash:
                raise ApiError(code='PIN_REQUIRED', message='Сначала задайте PIN из 4 цифр', status_code=400)
            if pref.auto_logout_minutes and pref.auto_lock_minutes >= pref.auto_logout_minutes:
                raise ApiError(code='INVALID_INTERVAL', message='Выход должен происходить позже блокировки', status_code=400)
            pref.save()
            UserSession.objects.filter(pk=request.auth.get('sid'), user=request.user).update(last_activity_at=timezone.now())
            if not pref.pin_hash:
                UserSession.objects.filter(user=request.user, revoked_at__isnull=True).update(locked_at=None)
        audit('auth.security_updated', actor=request.user, request=request, tenant_id=request.user.tenant_id)
        return Response(security_payload(pref))


class ActivityView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        UserSession.objects.filter(pk=request.auth.get('sid'), user=request.user, revoked_at__isnull=True,
            locked_at__isnull=True).update(last_activity_at=timezone.now())
        return Response({'active': True})


class LockView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        pref, _ = UserPreference.objects.get_or_create(user=request.user)
        if not pref.pin_hash:
            raise ApiError(code='PIN_REQUIRED', message='Сначала задайте PIN в настройках безопасности', status_code=400)
        UserSession.objects.filter(pk=request.auth.get('sid'), user=request.user).update(locked_at=timezone.now())
        return Response({'locked': True})


class UnlockView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        identifier = f'pin:{request.user.pk}'
        attempts = FailedLoginAttempt.objects.filter(identifier=identifier,
            attempted_at__gte=timezone.now() - timedelta(minutes=15))
        if attempts.count() >= 5:
            raise ApiError(code='PIN_RATE_LIMIT', message='Слишком много попыток. Войдите по паролю или повторите через 15 минут', status_code=429)
        pref, _ = UserPreference.objects.get_or_create(user=request.user)
        pin = str(request.data.get('pin', ''))
        if len(pin) != pref.pin_length or not pin.isascii() or not pin.isdigit() or not pref.pin_hash or not check_password(pin, pref.pin_hash):
            FailedLoginAttempt.objects.create(identifier=identifier)
            raise ApiError(code='INVALID_PIN', message='Неверный PIN', status_code=400)
        UserSession.objects.filter(pk=request.auth.get('sid'), user=request.user, revoked_at__isnull=True).update(
            locked_at=None, last_activity_at=timezone.now())
        attempts.delete()
        return Response({'locked': False})
