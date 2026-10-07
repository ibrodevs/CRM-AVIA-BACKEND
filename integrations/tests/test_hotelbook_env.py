import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from integrations.adapters import AdapterError
from integrations.hotelbook import HotelbookAdapter
from integrations.tests.test_hotelbook import context, gateway, hb_supplier  # noqa: F401,F811
from suppliers.models import Supplier, SupplierCredential

pytestmark = pytest.mark.django_db


@pytest.fixture
def local_env(settings, monkeypatch):
    settings.HBPRO_ALLOW_ENV_FALLBACK = True
    settings.SETTINGS_MODULE = "config.settings.dev"
    monkeypatch.setenv("HBPRO_LOGIN", "fixture-login")
    monkeypatch.setenv("HBPRO_PASSWORD", "fixture-password")
    monkeypatch.setenv("HBPRO_DEFAULT_CITIZENSHIP", "KG")
    monkeypatch.setenv("HBPRO_LOCALE", "ru")
    monkeypatch.setenv("HBPRO_PAY_FORM", "CASHLESS")


def test_env_fallback_is_unsaved_sandbox_and_scoped(tenant, other_tenant, local_env):
    supplier = Supplier.objects.create(tenant=tenant, name="Local HB")
    adapter = HotelbookAdapter()
    credential, config = adapter._config(context(tenant, supplier))
    assert credential._state.adding
    assert credential.environment == "sandbox"
    assert config["login"] == "fixture-login"
    assert not SupplierCredential.objects.exists()
    assert adapter._cache_key(credential, config, "token") == adapter._cache_key(
        *adapter._config(context(tenant, supplier)), "token"
    )
    with pytest.raises(AdapterError, match="credentials"):
        adapter._config(context(other_tenant, supplier))


@pytest.mark.parametrize(
    "module, enabled",
    [
        ("config.settings.prod", True),
        ("config.settings.pythonanywhere", True),
        ("config.settings.test", False),
    ],
)
def test_env_fallback_disabled_outside_explicit_local_mode(tenant, local_env, settings, module, enabled):
    settings.HBPRO_ALLOW_ENV_FALLBACK = enabled
    settings.SETTINGS_MODULE = module
    supplier = Supplier.objects.create(tenant=tenant, name="HB")
    with pytest.raises(AdapterError) as exc:
        HotelbookAdapter()._config(context(tenant, supplier))
    assert exc.value.code == "PROVIDER_NOT_CONFIGURED"


def test_db_credentials_take_priority_and_failure_does_not_use_env(tenant, hb_supplier, local_env):  # noqa: F811
    adapter = HotelbookAdapter()
    stored = SupplierCredential.objects.get(supplier=hb_supplier)
    stored.encrypted_secrets = json.dumps({"login": "database-login", "password": "database-password"})
    stored.save()
    credential, config = adapter._config(context(tenant, hb_supplier))
    assert credential.pk == stored.pk and not credential._state.adding
    assert config["login"] == "database-login"
    stored.status = "failed"
    stored.save()
    with pytest.raises(AdapterError):
        adapter._config(context(tenant, hb_supplier))


def test_local_diagnostic_is_read_only_and_rolls_back(tenant, local_env, gateway):  # noqa: F811
    out = io.StringIO()
    supplier_count = Supplier.objects.count()
    call_command("hotelbook_live_test", check_in="2027-03-10", check_out="2027-03-12", stdout=out)
    assert "login=OK" in out.getvalue()
    assert "results_count=1" in out.getvalue()
    assert "booking NOT sent" in out.getvalue()
    assert "fixture-password" not in out.getvalue() and "fixture-login" not in out.getvalue()
    assert not any("/orders" in path or "/book" in path for _, path, *_ in gateway.calls)
    assert Supplier.objects.count() == supplier_count
    assert not SupplierCredential.objects.exists()


def test_local_diagnostic_refuses_production(settings):
    settings.SETTINGS_MODULE = "config.settings.prod"
    with pytest.raises(CommandError, match="config.settings.dev"):
        call_command("hotelbook_live_test")


def test_details_amount_fines_normalize_for_ui():
    rules = HotelbookAdapter()._rules(
        {
            "finePolicies": {
                "cancel": [{"from": None, "amount": {"amount": 100, "currency": "EUR"}}],
                "change": [{"amount": {"amount": 80, "currency": "EUR"}}],
                "info": ["Late cancellation: 100%"],
            }
        }
    )
    assert rules["cancel"][0]["price"] == {"amount": 100, "currency": "EUR"}
    assert rules["change"][0]["price"]["amount"] == 80
    assert "100 EUR" in rules["cancellation_rules"]
    assert "Late cancellation: 100%" in rules["cancellation_rules"]
    assert rules["free_cancel_until"] is None
