"""Выпуск и ротация JWT с привязкой к UserSession."""
from datetime import timedelta

from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import User, UserSession

CHALLENGE_LIFETIME = timedelta(minutes=5)


def issue_session_tokens(user: User, request) -> dict:
    """Создаёт UserSession и пару токенов access/refresh."""
    refresh = RefreshToken.for_user(user)
    session = UserSession.objects.create(
        user=user,
        refresh_jti=refresh["jti"],
        ip_address=_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
        expires_at=timezone.now() + refresh.lifetime,
    )
    _stamp(refresh, user, session)
    access = refresh.access_token
    _stamp(access, user, session)
    return {"access": str(access), "refresh": str(refresh), "session": session}


def rotate_session_tokens(user: User, session: UserSession, old_refresh: RefreshToken) -> dict:
    """Ротация refresh: старый в blacklist, sid сохраняется."""
    old_refresh.blacklist()
    new_refresh = RefreshToken.for_user(user)
    session.refresh_jti = new_refresh["jti"]
    session.last_seen_at = timezone.now()
    session.expires_at = timezone.now() + new_refresh.lifetime
    session.save(update_fields=["refresh_jti", "last_seen_at", "expires_at"])
    _stamp(new_refresh, user, session)
    access = new_refresh.access_token
    _stamp(access, user, session)
    return {"access": str(access), "refresh": str(new_refresh)}


def issue_2fa_challenge(user: User) -> str:
    """Короткоживущий токен, дающий право только на /auth/2fa/verify/."""
    token = AccessToken.for_user(user)
    token.set_exp(lifetime=CHALLENGE_LIFETIME)
    token["scope"] = "2fa"
    return str(token)


def _stamp(token, user: User, session: UserSession) -> None:
    token["sid"] = str(session.id)
    token["tenant_id"] = str(user.tenant_id)


def _ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None
