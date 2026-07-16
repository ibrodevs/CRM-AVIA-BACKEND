import pytest
from rest_framework.test import APIClient

from accounts.management.commands.bootstrap_tenant import sync_system_roles
from accounts.models import Role, User, UserRole
from tenancy.models import Organization


@pytest.fixture
def tenant(db):
    org = Organization.objects.create(name="Test Org", slug="test-org")
    sync_system_roles(org)
    return org


@pytest.fixture
def other_tenant(db):
    org = Organization.objects.create(name="Other Org", slug="other-org")
    sync_system_roles(org)
    return org


def make_user(tenant, email, role_code=None, password="Str0ng-Pass-123!", **extra):
    user = User.objects.create_user(
        email=email, password=password, tenant=tenant, status=User.Status.ACTIVE, **extra
    )
    if role_code:
        role = Role.objects.get(tenant=tenant, code=role_code)
        UserRole.objects.create(user=user, role=role)
    return user


@pytest.fixture
def admin_user(tenant):
    return make_user(tenant, "admin@test.local", "admin")


@pytest.fixture
def operator_user(tenant):
    return make_user(tenant, "operator@test.local", "operator")


@pytest.fixture
def accountant_user(tenant):
    return make_user(tenant, "accountant@test.local", "accountant")


@pytest.fixture
def manager_user(tenant):
    return make_user(tenant, "manager@test.local", "manager")


def login(client: APIClient, email: str, password: str = "Str0ng-Pass-123!") -> dict:
    response = client.post("/api/v1/auth/login/", {"login": email, "password": password})
    assert response.status_code == 200, response.content
    return response.json()


def auth_client(user) -> APIClient:
    client = APIClient()
    tokens = login(client, user.email)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    client._tokens = tokens
    return client


@pytest.fixture
def admin_client(admin_user):
    return auth_client(admin_user)


@pytest.fixture
def operator_client(operator_user):
    return auth_client(operator_user)
