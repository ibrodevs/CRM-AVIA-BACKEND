import io
import pytest
from django.core.management import call_command
from accounts.models import Role, User
from tenancy.models import Organization
from orders.models import Order
from crm.models import ClientProfile, Company


@pytest.mark.django_db
def test_seed_demo_data_creates_and_aligns_users():
    # First seed
    call_command("seed_demo_data", force=True)

    org = Organization.objects.get(slug="travelhub")
    admin = User.objects.get(email="admin@travelhub.local")
    assert admin.tenant_id == org.id
    assert admin.is_staff is True
    assert admin.check_password("Demo-Pass-2026!") is True
    assert Role.objects.filter(user_roles__user=admin, code="admin", tenant=org).exists()

    assert Order.objects.filter(tenant=org).count() > 0
    assert ClientProfile.objects.filter(tenant=org).count() > 0
    assert Company.objects.filter(tenant=org).count() > 0

    # Simulate user moved to a different tenant accidentally
    other_org = Organization.objects.create(slug="other_org", name="Other Org")
    admin.tenant = other_org
    admin.save(update_fields=["tenant"])

    # Re-run seed_demo_data
    call_command("seed_demo_data", force=True)

    admin.refresh_from_db()
    assert admin.tenant_id == org.id
    assert Role.objects.filter(user_roles__user=admin, code="admin", tenant=org).exists()
    assert not Role.objects.filter(user_roles__user=admin, tenant=other_org).exists()


@pytest.mark.django_db
def test_diagnose_tenant_command_output():
    call_command("seed_demo_data", force=True)

    out = io.StringIO()
    call_command("diagnose_tenant", stdout=out)
    output = out.getvalue()

    assert "Travel Hub" in output
    assert "admin@travelhub.local" in output
    assert "Заказы:" in output

