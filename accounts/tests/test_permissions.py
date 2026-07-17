import pytest

from accounts.permissions import has_permission, user_permission_codes
from conftest import auth_client, make_user

pytestmark = pytest.mark.django_db


class TestRBAC:
    def test_admin_has_users_manage(self, admin_user):
        assert has_permission(admin_user, "users.manage")

    def test_operator_matrix(self, operator_user):
        codes = user_permission_codes(operator_user)
        assert "orders.create" in codes
        assert "services.book" in codes
        assert "finance.approve_payment" not in codes
        assert "users.manage" not in codes

    def test_accountant_matrix(self, accountant_user):
        codes = user_permission_codes(accountant_user)
        assert "finance.approve_payment" in codes
        assert "services.book" not in codes

        assert "crm.view_person_documents" not in codes

    def test_manager_cannot_pay_or_refund(self, manager_user):
        codes = user_permission_codes(manager_user)
        assert "finance.create_payment" not in codes
        assert "finance.refund" not in codes
        assert "services.refund" not in codes


class TestEndpointAccess:
    def test_operator_cannot_manage_users(self, operator_client):
        assert operator_client.get("/api/v1/users/").status_code == 403

    def test_admin_can_manage_users(self, admin_client):
        assert admin_client.get("/api/v1/users/").status_code == 200

    def test_anonymous_gets_401(self, db):
        from rest_framework.test import APIClient

        response = APIClient().get("/api/v1/me/")
        assert response.status_code == 401

    def test_user_roles_update(self, admin_client, tenant):
        target = make_user(tenant, "target@test.local")
        response = admin_client.put(
            f"/api/v1/users/{target.id}/roles/", {"roles": ["operator", "manager"]}, format="json"
        )
        assert response.status_code == 200
        assert set(response.json()["roles"]) == {"operator", "manager"}

    def test_unknown_role_rejected(self, admin_client, tenant):
        target = make_user(tenant, "target2@test.local")
        response = admin_client.put(
            f"/api/v1/users/{target.id}/roles/", {"roles": ["superhero"]}, format="json"
        )
        assert response.status_code == 400


class TestTenantIsolation:
    def test_admin_does_not_see_other_tenant_users(self, admin_client, other_tenant):
        make_user(other_tenant, "foreign@other.local")
        emails = [u["email"] for u in admin_client.get("/api/v1/users/").json()["results"]]
        assert "foreign@other.local" not in emails

    def test_cannot_manage_other_tenant_user(self, admin_client, other_tenant):
        foreign = make_user(other_tenant, "foreign2@other.local")
        assert admin_client.get(f"/api/v1/users/{foreign.id}/").status_code == 404

    def test_events_isolated_by_tenant(self, admin_user, other_tenant):
        from common.outbox import emit_event
        from tenancy.context import tenant_context

        with tenant_context(other_tenant.id):
            emit_event("order.updated", "Order", tenant_id=other_tenant.id)
        client = auth_client(admin_user)
        events = client.get("/api/v1/events/?cursor=0").json()["events"]
        assert events == []
