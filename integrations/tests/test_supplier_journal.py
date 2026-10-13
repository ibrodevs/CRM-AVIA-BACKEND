import pytest

from integrations.models import IntegrationIncident, IntegrationLog
from suppliers.models import Supplier

pytestmark = pytest.mark.django_db


def test_supplier_journal_filters_supplier_and_tenant(admin_client, tenant, other_tenant):
    supplier = Supplier.objects.create(tenant=tenant, name="Selected")
    another = Supplier.objects.create(tenant=tenant, name="Another")
    foreign = Supplier.objects.create(tenant=other_tenant, name="Foreign")
    selected_log = None
    selected_incident = None
    for owner, provider in [(tenant, supplier), (tenant, another), (other_tenant, foreign)]:
        log = IntegrationLog.objects.create(
            tenant=owner, supplier=provider, provider_adapter="mock", operation="search",
            result="error", http_status=503, duration_ms=250, retries=2,
            request_sanitized={"kind": "hotel"}, response_sanitized={"status": "unavailable"},
        )
        incident = IntegrationIncident.objects.create(
            tenant=owner, supplier=provider, operation="search", error_code="UNAVAILABLE",
        )
        if provider == supplier:
            selected_log, selected_incident = log, incident
    logs = admin_client.get(f"/api/v1/integration-operations/?supplier={supplier.pk}")
    assert logs.status_code == 200
    assert [row["id"] for row in logs.json()["results"]] == [selected_log.pk]
    row = logs.json()["results"][0]
    assert row["request_sanitized"] == {"kind": "hotel"}
    assert row["response_sanitized"] == {"status": "unavailable"}
    assert row["retries"] == 2
    incidents = admin_client.get(f"/api/v1/integration-incidents/?supplier={supplier.pk}")
    assert incidents.status_code == 200
    assert [row["id"] for row in incidents.json()["results"]] == [selected_incident.pk]
    for endpoint in ["integration-operations", "integration-incidents"]:
        response = admin_client.get(f"/api/v1/{endpoint}/?supplier={foreign.pk}")
        assert response.status_code == 200
        assert response.json()["results"] == []
