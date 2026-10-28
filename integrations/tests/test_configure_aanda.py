"""Interactive setup safety: synthetic secrets and a stubbed read-only gateway."""

import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from integrations.aanda_setup import select_supplier
from integrations.models import IntegrationLog
from suppliers.models import Supplier, SupplierCredential
from tenancy.models import Organization

pytestmark = pytest.mark.django_db
PASSWORD = 'test-only-secret-<&>"'


def interactive(monkeypatch):
    prompts = []
    answers = iter(["HMEL2024", "hmel"])

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    return prompts


def gateway(monkeypatch, response=None):
    calls = []

    def exchange(url, body):
        from urllib.parse import parse_qs
        from xml.etree import ElementTree as ET

        params = parse_qs(body.decode())
        assert url == "https://go-api.aanda.ru/"
        assert params["RequestName"] == ["CurrencyListRequest"]
        root = ET.fromstring(params["XML"][0])
        assert root.get("UserId") == "hmel"
        assert root.get("Password") == PASSWORD
        calls.append(params["RequestName"][0])
        return response or (200, b"<CurrencyListResponse><Success/></CurrencyListResponse>")

    monkeypatch.setattr("integrations.aanda_xml.exchange", exchange)
    return calls


def test_create_then_update_three_prompts_and_encrypted_readonly_check(tenant, monkeypatch, settings):
    settings.AANDA_ENVIRONMENT = "production"  # command must pin sandbox only
    prompts = interactive(monkeypatch)
    calls = gateway(monkeypatch)
    output = io.StringIO()
    call_command("configure_aanda", stdout=output)
    supplier = Supplier.objects.get(name="A&A / Roomlink")
    assert supplier.service_kinds == ["hotel"] and supplier.status == "active"
    assert supplier.automation_capabilities == {"adapter": "aanda"}
    credential = supplier.credentials.get()
    assert credential.environment == "sandbox" and credential.status == "active"
    assert json.loads(credential.encrypted_secrets) == {
        "buyer_id": "HMEL2024",
        "user_id": "hmel",
        "password": PASSWORD,
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT encrypted_secrets FROM suppliers_credential WHERE id=%s", [credential.pk])
        raw = cursor.fetchone()[0]
    assert raw.startswith("enc$1$") and PASSWORD not in raw
    # Existing A&A wins even when another tenant exists.
    Organization.objects.create(name="Isolation test", slug="isolation")
    interactive(monkeypatch)
    call_command("configure_aanda", stdout=output)
    assert SupplierCredential.objects.count() == 1
    assert supplier.credentials.get().pk == credential.pk
    assert prompts == ["BuyerID: ", "Login / UserID: "]
    assert calls == ["CurrencyListRequest", "CurrencyListRequest"]
    assert PASSWORD not in output.getvalue()
    assert "A&A AUTH: OK" in output.getvalue()
    assert PASSWORD not in json.dumps(list(IntegrationLog.objects.values()), default=str)
    assert settings.AANDA_ENVIRONMENT == "production"


def test_auth_error_cannot_echo_password(tenant, monkeypatch, caplog):
    interactive(monkeypatch)
    gateway(monkeypatch, (403, f'<Error Code="20" Description="{PASSWORD}"/>'.encode()))
    output = io.StringIO()
    with pytest.raises(CommandError) as error:
        call_command("configure_aanda", stdout=output)
    assert "HTTP status: 403" in str(error.value)
    assert PASSWORD not in str(error.value) + output.getvalue() + caplog.text
    assert PASSWORD not in json.dumps(list(IntegrationLog.objects.values()), default=str)
    assert SupplierCredential.objects.get().status == "failed"


def test_check_auto_selects_without_supplier_argument(tenant, monkeypatch):
    interactive(monkeypatch)
    gateway(monkeypatch)
    call_command("configure_aanda", stdout=io.StringIO())
    output = io.StringIO()
    call_command("aanda_sandbox_check", stdout=output)
    assert "Sandbox connection: OK" in output.getvalue()


def test_ambiguous_selection_does_not_guess(tenant, other_tenant):
    for org in (tenant, other_tenant):
        Supplier.objects.create(tenant=org, name="A&A")
    with pytest.raises(CommandError, match="Несколько"):
        select_supplier()


def test_non_tty_refused_before_secret_input(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(CommandError, match="терминале"):
        call_command("configure_aanda")
