from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from accounts.models import User, UserSession
from tenancy.context import set_current_tenant_id


class SessionAwareJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        if validated_token.get("scope") == "2fa":
            raise AuthenticationFailed("Токен 2FA-challenge не даёт доступа к API", code="2fa_challenge")

        user = super().get_user(validated_token)
        if user.status != User.Status.ACTIVE:
            raise AuthenticationFailed("Учётная запись неактивна", code="user_inactive")

        sid = validated_token.get("sid")
        if sid:
            session = UserSession.objects.filter(pk=sid).only("revoked_at", "expires_at").first()
            if session is None or not session.is_active:
                raise AuthenticationFailed("Сессия завершена", code="session_revoked")

        set_current_tenant_id(user.tenant_id)
        return user
