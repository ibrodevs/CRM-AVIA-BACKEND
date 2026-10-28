"""Regressions discovered during the live Roomlink acceptance review."""

import json
from xml.etree import ElementTree as ET

import pytest

from integrations.models import IntegrationLog
from integrations.tests.test_aanda import aa_gateway as aa_gateway
from integrations.tests.test_aanda import aa_supplier as aa_supplier
from integrations.tests.test_aanda import ctx as ctx
from integrations.tests.test_aanda import ready_booking, search, start
from services.hotel_presentation import hotel_service_details

pytestmark = pytest.mark.django_db


def test_hotel_times_news_and_optional_meal_not_claimed_included(ctx, aa_gateway, monkeypatch):
    boundary = aa_gateway

    def exchange(url, body):
        status, data = boundary(url, body)
        root = ET.fromstring(data)
        hotel = root.find("./AvailableHotelsList/Hotel")
        if hotel is not None:
            news = ET.SubElement(
                ET.SubElement(hotel, "NewsList"),
                "News",
                {
                    "NewsID": "123",
                    "NewsTitle": "Важная информация",
                    "NewsInfo": "Ресепшен до 17:00",
                    "NewsBegin": "2026-01-01",
                    "NewsEnd": "2030-01-01",
                },
            )
            ET.SubElement(news, "Unexpected", Password="never retain")
            meal = hotel.find(".//Meal")
            meal.set("Code", "2")
        return status, ET.tostring(root)

    monkeypatch.setattr("integrations.aanda_xml.exchange", exchange)
    offer = search(ctx)[0]
    assert offer["itinerary"]["check_in_time"] == "14:00"
    assert offer["itinerary"]["check_out_time"] == "12:00"
    assert offer["itinerary"]["meal_plan"] == "Питание не включено"
    assert offer["fare"]["meal_information"][0]["included"] is False
    assert offer["fare"]["pay_at_hotel"] is None
    assert offer["fare"]["information"][0]["text"] == "Ресепшен до 17:00"
    assert "Unexpected" not in json.dumps(offer)
    assert IntegrationLog.objects.latest("created_at").external_ids["Hotels"] == "453"


def test_special_requests_through_crm_workflow_and_revalidation(
    admin_client, tenant, aa_supplier, aa_gateway
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier, preflight=False)
    remark = "Пожелание: тихий номер & <верхний этаж>"
    response = admin_client.put(
        f"/api/v1/services/{service.pk}/passengers/",
        {
            "version": service.version,
            "participants": [str(row.participant_id) for row in service.passengers.all()],
            "special_requests": remark,
        },
        format="json",
    )
    assert response.status_code == 200
    response = admin_client.post(f"/api/v1/booking-workflows/{workflow['id']}/preflight/", {}, format="json")
    assert response.status_code == 200 and response.json()["ok"]
    service.refresh_from_db()
    assert service.provider_snapshot["special_requests"] == remark
    assert start(admin_client, workflow)["status"] == "completed"
    submitted = next(root for name, root in aa_gateway.calls if name == "OrderRequest")
    assert submitted.find(".//Accommodation").get("AdditionalInfo") == remark
    assert remark not in json.dumps(list(IntegrationLog.objects.values()), default=str)
    overview = admin_client.get(f"/api/v1/orders/{service.order_id}/overview/").json()
    assert overview["services"][0]["hotel_details"]["special_requests"] == remark
    assert overview["services"][0]["hotel_details"]["itinerary"]["check_in_time"] == "14:00"


def test_client_projection_excludes_provider_auth_and_guest_fields(ctx, aa_gateway):
    offer = search(ctx)[0]
    offer["aanda_booking"] = {
        "order_id": "1",
        "items": [{"confirmation_number": "CN1", "Persons": "private"}],
    }
    offer["password"] = "private"
    result = hotel_service_details(offer)
    assert result["accommodations"][0]["confirmation_number"] == "CN1"
    assert "credential_id" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    assert hotel_service_details({"provider_adapter": "hotelbook"}) is None


def test_confirmation_and_notices_survive_cancel(admin_client, tenant, aa_supplier, aa_gateway, monkeypatch):
    from integrations.aanda import AandaAdapter
    from integrations.adapters import AdapterContext

    boundary = aa_gateway

    def exchange(url, body):
        status, data = boundary(url, body)
        root = ET.fromstring(data)
        if root.tag == "OrderInfoResponse":
            room = root.find(".//Accommodation")
            if aa_gateway.status != "71":
                room.set("ConfirmationNumber", "CONFIRMED-1")
            ET.SubElement(
                ET.SubElement(root, "CheckinNotice"),
                "ObtainingKeys",
                {
                    "Type": "Ресепшен",
                    "Time": "09:00–17:00",
                    "Password": "never retain",
                },
            )
        return status, ET.tostring(root)

    monkeypatch.setattr("integrations.aanda_xml.exchange", exchange)
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    start(admin_client, workflow)
    service.refresh_from_db()
    ctx = AdapterContext(tenant_id=tenant.pk, supplier_id=aa_supplier.pk)
    result = AandaAdapter().cancel(ctx, service.external_id)
    assert result["status"] == "cancelled"
    booking = result["provider_snapshot"]["aanda_booking"]
    assert booking["items"][0]["confirmation_number"] == "CONFIRMED-1"
    assert booking["checkin_notice"]["ObtainingKeys"]["Type"] == "Ресепшен"
    assert "never retain" not in json.dumps(result)


def test_offer_revalidation_renews_persisted_expiry(ctx, aa_gateway, tenant, admin_user):
    from django.utils import timezone

    from integrations.aanda import AandaAdapter
    from services.job_handlers import _persist_offer
    from services.models import SearchSession, ServiceOffer
    from services.revalidation import update_offer

    raw = search(ctx)[0]
    session = SearchSession.objects.create(tenant=tenant, user=admin_user, kind="hotel", criteria={})
    assert _persist_offer(session, aa_gateway_supplier(ctx), "aanda", raw, set())
    offer = ServiceOffer.objects.get(session=session)
    offer.expires_at = timezone.now() - timezone.timedelta(minutes=1)
    offer.save(update_fields=["expires_at"])
    update_offer(offer, AandaAdapter().revalidate(ctx, raw))
    offer.refresh_from_db()
    assert offer.expires_at > timezone.now()


def aa_gateway_supplier(ctx):
    from suppliers.models import Supplier

    return Supplier.objects.get(pk=ctx.supplier_id)


def test_partial_city_suggestion_never_becomes_exhaustive_index(ctx, aa_gateway, monkeypatch):
    from django.core.cache import cache

    from integrations.aanda import AandaAdapter

    cache.clear()
    calls = []

    def exchange(url, body):
        calls.append(body)
        if b"CountryListRequest" in body:
            return (
                200,
                b'<CountryList><Success/><Country Code="9" Name="Russia"/><Country Code="10" Name="Other"/></CountryList>',
            )
        return 200, b'<CityList><Success/><Country><City Code="18" Name="Samara"/></Country></CityList>'

    monkeypatch.setattr("integrations.aanda_xml.exchange", exchange)
    adapter = AandaAdapter()
    assert len(adapter.locations(ctx, "Samara", partial=True)) == 1
    assert len(calls) == 2
    # Full matching must still inspect the second country and see ambiguity.
    assert len(adapter.locations(ctx, "Samara")) == 2
    assert len(calls) == 3
    cache.clear()


def test_selected_location_routes_only_its_supplier_and_provider(tenant, other_tenant, aa_supplier):
    from services.job_handlers import _suppliers_for_search
    from services.models import SearchSession
    from suppliers.models import Supplier, SupplierCredential

    unrelated = Supplier.objects.create(tenant=tenant, name="Hotelbook", service_kinds=["hotel"])
    SupplierCredential.objects.create(tenant=tenant, supplier=unrelated, provider_adapter="hotelbook")
    criteria = {"provider_selection": {"provider_adapter": "aanda", "supplier_id": str(aa_supplier.pk)}}
    assert _suppliers_for_search(SearchSession(tenant=tenant, kind="hotel", criteria=criteria)) == [
        aa_supplier
    ]
    assert _suppliers_for_search(SearchSession(tenant=other_tenant, kind="hotel", criteria=criteria)) == []
    criteria["provider_selection"]["provider_adapter"] = "hotelbook"
    assert _suppliers_for_search(SearchSession(tenant=tenant, kind="hotel", criteria=criteria)) == []


def test_selected_provider_does_not_call_second_adapter_on_same_supplier(
    tenant, admin_user, aa_supplier, aa_gateway
):
    from common.models import BackgroundJob
    from integrations.tests.test_aanda import CRITERIA
    from services.job_handlers import run_search
    from services.models import SearchSession
    from suppliers.models import SupplierCredential

    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=aa_supplier,
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets="{}",
    )
    session = SearchSession.objects.create(
        tenant=tenant,
        user=admin_user,
        kind="hotel",
        criteria={
            **CRITERIA,
            "provider_selection": {"supplier_id": str(aa_supplier.pk), "provider_adapter": "aanda"},
        },
    )
    job = BackgroundJob.objects.create(
        tenant=tenant, kind="services.search", payload={"session_id": str(session.pk)}
    )
    result = run_search(job)
    assert result["failed"] == 0 and result["succeeded"] == 1
    assert list(session.provider_runs.values_list("provider_adapter", flat=True)) == ["aanda"]
