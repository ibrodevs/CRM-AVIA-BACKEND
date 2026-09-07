import pytest
from rest_framework.test import APIClient


@pytest.mark.parametrize("origin", ["https://external.example", "http://localhost:5173", "null"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def test_external_preflight_allows_api_headers_without_login(origin, method):
    response = APIClient().options(
        "/api/v1/orders/",
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type,idempotency-key,x-request-id,if-none-match",
    )
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == "*"
    assert method in response["Access-Control-Allow-Methods"]
    allowed = response["Access-Control-Allow-Headers"].lower()
    for header in ["authorization", "content-type", "idempotency-key", "x-request-id", "if-none-match"]:
        assert header in allowed
    assert "Access-Control-Allow-Credentials" not in response


def test_cors_does_not_apply_to_admin():
    response = APIClient().get("/admin/login/", HTTP_ORIGIN="https://external.example")
    assert "Access-Control-Allow-Origin" not in response


def test_origin_allowlist_can_be_enabled(settings):
    settings.CORS_ALLOW_ALL_ORIGINS = False
    settings.CORS_ALLOWED_ORIGINS = ["https://trusted.example"]
    client = APIClient()
    for origin, allowed in [("https://trusted.example", True), ("https://external.example", False)]:
        response = client.options(
            "/api/v1/me/", HTTP_ORIGIN=origin, HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET"
        )
        assert ("Access-Control-Allow-Origin" in response) is allowed
        if allowed:
            assert response["Access-Control-Allow-Origin"] == origin


@pytest.mark.django_db
def test_external_requests_still_require_valid_authentication():
    client = APIClient()
    for headers in [{}, {"HTTP_AUTHORIZATION": "Bearer invalid"}]:
        response = client.get("/api/v1/me/", HTTP_ORIGIN="https://external.example", **headers)
        assert response.status_code == 401
        assert response["Access-Control-Allow-Origin"] == "*"


@pytest.mark.django_db
def test_external_login_and_authenticated_request(admin_user):
    client = APIClient()
    response = client.post(
        "/api/v1/auth/login/",
        {"login": admin_user.email, "password": "Str0ng-Pass-123!"},
        format="json",
        HTTP_ORIGIN="https://external.example",
    )
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == "*"
    tokens = response.json()
    response = client.get(
        "/api/v1/me/",
        HTTP_ORIGIN="https://another.example",
        HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
    )
    assert response.status_code == 200
    assert response.json()["email"] == admin_user.email
    assert response["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response
    for header in ["X-Request-ID", "Content-Disposition", "ETag", "Retry-After"]:
        assert header in response["Access-Control-Expose-Headers"]
    refreshed = client.post(
        "/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]},
        format="json", HTTP_ORIGIN="https://another.example",
    )
    assert refreshed.status_code == 200
    assert refreshed["Access-Control-Allow-Origin"] == "*"


@pytest.mark.django_db
def test_external_origin_does_not_bypass_role_permissions(operator_client):
    response = operator_client.get("/api/v1/users/", HTTP_ORIGIN="https://external.example")
    assert response.status_code == 403
    assert response["Access-Control-Allow-Origin"] == "*"
