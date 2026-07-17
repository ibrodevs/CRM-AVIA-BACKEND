import pyotp
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import TwoFactorConfig, UserSession
from conftest import auth_client, login

pytestmark = pytest.mark.django_db


class TestLogin:
    def test_login_success(self, admin_user):
        tokens = login(APIClient(), admin_user.email)
        assert "access" in tokens and "refresh" in tokens
        assert UserSession.objects.filter(user=admin_user, revoked_at__isnull=True).count() == 1

    def test_login_wrong_password(self, admin_user):
        response = APIClient().post("/api/v1/auth/login/", {"login": admin_user.email, "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_login_suspended_user(self, admin_user):
        admin_user.status = "suspended"
        admin_user.save(update_fields=["status"])
        response = APIClient().post(
            "/api/v1/auth/login/", {"login": admin_user.email, "password": "Str0ng-Pass-123!"}
        )
        assert response.status_code == 401

    def test_brute_force_lockout(self, admin_user):
        client = APIClient()
        for _ in range(10):
            client.post("/api/v1/auth/login/", {"login": admin_user.email, "password": "wrong"})
        response = client.post(
            "/api/v1/auth/login/", {"login": admin_user.email, "password": "Str0ng-Pass-123!"}
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"

    def test_error_contract_shape(self, admin_user):
        response = APIClient().post("/api/v1/auth/login/", {"login": admin_user.email, "password": "wrong"})
        body = response.json()["error"]
        assert set(body) == {"code", "message", "fields", "details", "request_id"}


class TestTwoFactor:
    def test_2fa_flow(self, admin_user):
        secret = pyotp.random_base32()
        TwoFactorConfig.objects.create(user=admin_user, totp_secret=secret, confirmed_at=timezone.now())
        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/", {"login": admin_user.email, "password": "Str0ng-Pass-123!"}
        )
        body = response.json()
        assert body["two_factor_required"] is True
        challenge = body["challenge_token"]

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {challenge}")
        assert client.get("/api/v1/me/").status_code == 401
        client.credentials()

        response = client.post("/api/v1/auth/2fa/verify/", {"challenge_token": challenge, "code": "000000"})
        assert response.status_code == 401

        code = pyotp.TOTP(secret).now()
        response = client.post("/api/v1/auth/2fa/verify/", {"challenge_token": challenge, "code": code})
        assert response.status_code == 200
        assert "access" in response.json()


class TestSessions:
    def test_refresh_rotation(self, admin_user):
        client = APIClient()
        tokens = login(client, admin_user.email)
        response = client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
        assert response.status_code == 200
        new_tokens = response.json()

        response = client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
        assert response.status_code == 401

        response = client.post("/api/v1/auth/token/refresh/", {"refresh": new_tokens["refresh"]})
        assert response.status_code == 200

    def test_logout_revokes_access(self, admin_user):
        client = auth_client(admin_user)
        assert client.get("/api/v1/me/").status_code == 200
        assert client.post("/api/v1/auth/logout/").status_code == 204
        assert client.get("/api/v1/me/").status_code == 401

    def test_logout_all_keeps_current(self, admin_user):
        other = auth_client(admin_user)
        current = auth_client(admin_user)
        response = current.post("/api/v1/auth/logout-all/")
        assert response.status_code == 200
        assert response.json()["revoked_sessions"] == 1
        assert current.get("/api/v1/me/").status_code == 200
        assert other.get("/api/v1/me/").status_code == 401

    def test_sessions_list_and_delete(self, admin_user):
        client = auth_client(admin_user)
        auth_client(admin_user)
        response = client.get("/api/v1/auth/sessions/")
        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 2
        other_id = next(s["id"] for s in results if not s["is_current"])
        assert client.delete(f"/api/v1/auth/sessions/{other_id}/").status_code == 204
        assert len(client.get("/api/v1/auth/sessions/").json()["results"]) == 1


class TestPassword:
    def test_change_password_revokes_other_sessions(self, admin_user):
        other = auth_client(admin_user)
        client = auth_client(admin_user)
        response = client.post(
            "/api/v1/auth/password/change/",
            {
                "current_password": "Str0ng-Pass-123!",
                "new_password": "N3w-Strong-Pass-456!",
            },
        )
        assert response.status_code == 204
        assert other.get("/api/v1/me/").status_code == 401
        assert client.get("/api/v1/me/").status_code == 200
        login(APIClient(), admin_user.email, "N3w-Strong-Pass-456!")

    def test_reset_request_does_not_reveal_account(self, db):
        response = APIClient().post("/api/v1/auth/password/reset/request/", {"email": "nobody@nowhere.test"})
        assert response.status_code == 200

    def test_weak_password_rejected(self, admin_user):
        client = auth_client(admin_user)
        response = client.post(
            "/api/v1/auth/password/change/",
            {
                "current_password": "Str0ng-Pass-123!",
                "new_password": "123",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "WEAK_PASSWORD"
