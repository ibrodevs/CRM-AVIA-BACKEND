import pytest

from suppliers.models import SupplierCredential

pytestmark = pytest.mark.django_db


def create_supplier(admin_client):
    response = admin_client.post(
        "/api/v1/suppliers/",
        {
            "name": "Test Provider",
            "legal_name": "Test Provider LLC",
            "status": "active",
            "is_global": True,
            "service_kinds": ["avia"],
            "currencies": ["USD"],
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def test_connection_check_reports_missing_credentials(admin_client):
    supplier = create_supplier(admin_client)

    response = admin_client.post(f"/api/v1/suppliers/{supplier['id']}/check-connection/", {}, format="json")

    assert response.status_code == 409
    assert response.json()["status"] == "not_configured"


def test_connection_check_verifies_registered_adapter_and_secrets(admin_client):
    supplier = create_supplier(admin_client)
    credential = admin_client.post(
        f"/api/v1/suppliers/{supplier['id']}/credentials/",
        {
            "provider_adapter": "mock",
            "environment": "sandbox",
            "secrets": {"api_key": "sandbox-key"},
        },
        format="json",
    )
    assert credential.status_code == 201, credential.content

    response = admin_client.post(f"/api/v1/suppliers/{supplier['id']}/check-connection/", {}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "connected"
    assert response.json()["checked"][0]["provider_adapter"] == "mock"
    saved = SupplierCredential.objects.get(pk=credential.json()["id"])
    assert saved.status == "active"
    assert saved.last_verified_at is not None


def test_connection_check_rejects_unknown_adapter(admin_client):
    supplier = create_supplier(admin_client)
    credential = admin_client.post(
        f"/api/v1/suppliers/{supplier['id']}/credentials/",
        {
            "provider_adapter": "not-installed",
            "environment": "sandbox",
            "secrets": {"token": "value"},
        },
        format="json",
    )
    assert credential.status_code == 201, credential.content

    response = admin_client.post(f"/api/v1/suppliers/{supplier['id']}/check-connection/", {}, format="json")

    assert response.status_code == 409
    assert response.json()["status"] == "failed"
    assert response.json()["checked"][0]["result"] == "unknown_adapter"


def test_search_priority_round_trip_uses_supplier_ids(admin_client):
    first = create_supplier(admin_client)
    second = create_supplier(admin_client)

    saved = admin_client.post(
        "/api/v1/supplier-search-priorities/",
        {
            "service_kind": "avia",
            "ordered_suppliers": [second["id"], first["id"]],
            "fallback_supplier": first["id"],
            "conditions": {"configured_from": "crm_ui"},
            "is_active": True,
        },
        format="json",
    )

    assert saved.status_code == 201, saved.content
    payload = admin_client.get("/api/v1/supplier-search-priorities/").json()
    assert payload[0]["ordered_suppliers"] == [second["id"], first["id"]]
    assert payload[0]["fallback_supplier"] == first["id"]
