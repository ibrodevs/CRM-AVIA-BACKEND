import pytest

from crm.models import Company, SettlementProfile

pytestmark = pytest.mark.django_db


def test_empty_settlement_profile_does_not_hide_create_form(admin_client, admin_user, tenant):
    company = Company.objects.create(
        tenant=tenant,
        legal_name="ОсОО Без договора",
        short_name="Без договора",
        created_by=admin_user,
    )
    SettlementProfile.objects.create(
        tenant=tenant,
        company=company,
        mode=SettlementProfile.Mode.PREPAYMENT,
        created_by=admin_user,
    )

    response = admin_client.get(f"/api/v1/companies/{company.id}/financial-conditions/")

    assert response.status_code == 200, response.content
    assert response.json() == {"configured": False, "value": None}
