# ruff: noqa: F401, F811
"""Supplier HTTP is mocked: no financial or live HBPRO calls."""

import csv
import io
from copy import deepcopy

import pytest
from pypdf import PdfReader

from documents.models import Document, DocumentVersion
from integrations.tests.test_hotelbook import (
    ITEM_ID,
    Response,
    context,
    gateway,
    hb_supplier,
    ready_booking,
    start,
)
from services.models import OrderService, ServiceOffer

pytestmark = pytest.mark.django_db


@pytest.fixture
def content_gateway(gateway, monkeypatch):
    original = gateway.open

    def open_content(request, timeout):
        if "/dict/gateway/hotel/rooms?" in request.full_url:
            gateway.calls.append((request.method, "hotel/rooms", {}, None, {}))
            assert "hotelId=1251539" in request.full_url
            return Response(
                [
                    {
                        "id": "hb-room",
                        "hotelId": 1251539,
                        "name": "Стандарт",
                        "description": "<p>Две кровати</p>",
                        "amenityIds": [8],
                        "images": [
                            {
                                "variants": {
                                    "primary": "https://images.example/room.jpg",
                                    "preview": "https://images.example/room-small.jpg",
                                }
                            }
                        ],
                    },
                    {"id": "deleted", "hotelId": 1251539, "deletedAt": "2026-01-01"},
                    {"id": "wrong", "hotelId": 131687},
                    {
                        "id": "no-photo",
                        "hotelId": 1251539,
                        "name": "=HYPERLINK(1)",
                        "images": [{"variants": {"primary": "javascript:alert(1)"}}],
                    },
                ]
            )
        result = original(request, timeout)
        if "/hotel/hotels/" in request.full_url:
            result.payload["photos"] = [
                {
                    "photoUrl": "https://images.example/hotel.jpg",
                    "thumbnailUrl": "https://images.example/hotel-small.jpg",
                    "isMain": True,
                }
            ]
            result.payload["roomAmenities"] = [{"id": 8, "description": "Wi-Fi"}]
        return result

    monkeypatch.setattr(gateway, "open", open_content)
    return gateway


def test_supplier_content_cached_exported_and_scoped(
    admin_client, tenant, hb_supplier, content_gateway, other_tenant
):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    offer = ServiceOffer.objects.first()
    service = OrderService.objects.get(pk=service_data["id"])
    before = deepcopy(service.provider_snapshot)
    content_gateway.calls.clear()
    offer_url = f"/api/v1/service-offers/{offer.id}/hotel-content/"
    response = admin_client.get(offer_url)
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["selected_room_ids"] == ["hb-room"]
    assert len(data["rooms"]) == 2
    assert data["rooms"][0]["description"] == "Две кровати"
    assert data["rooms"][0]["amenities"] == ["Wi-Fi"]
    assert data["rooms"][0]["photos"][0]["url"].endswith("/room.jpg")
    assert data["hotel"]["photos"][0]["url"].endswith("/hotel.jpg")
    assert data["rooms"][1]["photos"] == []
    service_url = f"/api/v1/services/{service.id}/hotel-content/"
    assert admin_client.get(service_url).json() == data
    export = admin_client.get(service_url + "?export=csv")
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert rows[1][2] == "Стандарт" and rows[1][-1].endswith("room.jpg")
    assert rows[2][2].startswith("'=HYPERLINK")
    assert sum(c[1] == "hotel/rooms" for c in content_gateway.calls) == 1
    assert not any(c[0] == "POST" for c in content_gateway.calls)
    service.refresh_from_db()
    assert service.provider_snapshot == before
    overview = admin_client.get(f"/api/v1/orders/{service.order_id}/overview/").json()
    assert overview["services"][0]["hotel_documents"]["content_available"]
    offer.tenant = other_tenant
    offer.save()
    assert admin_client.get(offer_url).status_code == 404
    service.tenant = other_tenant
    service.save()
    assert admin_client.get(service_url).status_code == 404


def test_certificate_pdf_saved_once_and_cancelled_version_is_explicit(
    admin_client, tenant, hb_supplier, gateway, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=service_data["id"])
    url = f"/api/v1/services/{service.id}/hotel-certificate/"
    assert admin_client.post(url, {}).status_code == 409
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    gateway.calls.clear()
    response = admin_client.post(url, {})
    assert response.status_code == 200, response.content
    document = response.json()
    assert document["kind"] == "voucher" and document["service"] == str(service.id)
    assert admin_client.post(url, {}).json()["id"] == document["id"]
    assert DocumentVersion.objects.filter(document_id=document["id"]).count() == 1
    download_url = f"/api/v1/documents/{document['id']}/download/"
    download = admin_client.get(download_url)
    content = b"".join(download.streaming_content)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)
    assert "СЕРТИФИКАТ НА ПРОЖИВАНИЕ" in text and "IVANOV PETR" in text
    assert "2027-03-10" in text
    service.refresh_from_db()
    booking = service.provider_snapshot["hotelbook_booking"]
    for private_id in [
        "HBPRO",
        "HB-FIXTURE-REF",
        str(booking["order_id"]),
        booking["client_request_id"],
        ITEM_ID,
    ]:
        assert private_id not in text
    assert "120.25" not in text and "fixture-password" not in text
    assert "не оригинальный ваучер" in " ".join(text.split())
    assert not gateway.calls
    # Simulate a status sync only in local DB; never send cancellation to HBPRO.
    service.refresh_from_db()
    snapshot = service.provider_snapshot
    snapshot["hotelbook_booking"]["items"][0]["status"] = "CANCELED"
    service.provider_snapshot = snapshot
    service.save()
    assert admin_client.get(download_url).status_code == 409
    cancelled = admin_client.post(url, {})
    assert cancelled.status_code == 200
    assert cancelled.json()["id"] == document["id"]
    assert DocumentVersion.objects.filter(document_id=document["id"]).count() == 2
    content = b"".join(admin_client.get(download_url).streaming_content)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)
    assert "ОТМЕНЕНО" in text and "НЕ ДЕЙСТВУЕТ" in text
    assert admin_client.get(download_url + "?file_version=1").status_code == 409
    assert not gateway.calls


def test_certificate_enforces_permissions_and_tenant(
    admin_client, operator_client, operator_user, tenant, hb_supplier, gateway, other_tenant
):
    from accounts.models import RolePermission

    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=service_data["id"])
    url = f"/api/v1/services/{service.id}/hotel-certificate/"
    # Own order is required even within the same tenant.
    assert operator_client.post(url, {}).status_code == 404
    RolePermission.objects.filter(
        role__code="operator", role__tenant=tenant, permission_code="documents.generate"
    ).delete()
    assert operator_client.post(url, {}).status_code == 403
    service.tenant = other_tenant
    service.save()
    assert admin_client.post(url, {}).status_code == 404
    assert not Document.objects.exists()


def test_demo_refuses_real_database_and_blocks_supplier_mutations(settings):
    from urllib.error import URLError
    from urllib.request import Request

    from django.core.management.base import CommandError

    from integrations.management.commands.hbpro_ui_demo import Command, DemoGateway

    settings.DEBUG = False
    with pytest.raises(CommandError, match="Demo requires"):
        Command().handle()
    with pytest.raises(URLError, match="mutations are blocked"):
        DemoGateway().open(Request("https://api.hbpro.expert/api/v1/ru/hotel/gateway/book", data=b"{}"), 1)
    with pytest.raises(URLError, match="unsupported supplier request"):
        DemoGateway().open(Request("https://api.hbpro.expert/anything"), 1)


def test_old_certificate_format_requires_regeneration_before_download(
    admin_client, tenant, hb_supplier, gateway, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    url = f"/api/v1/services/{service_data['id']}/hotel-certificate/"
    document_id = admin_client.post(url, {}).json()["id"]
    document = Document.objects.get(pk=document_id)
    document.metadata.pop("certificate_schema_version")
    document.save(update_fields=["metadata"])
    download_url = f"/api/v1/documents/{document_id}/download/"
    assert admin_client.get(download_url).status_code == 409
    assert admin_client.post(url, {}).status_code == 200
    assert DocumentVersion.objects.filter(document_id=document_id).count() == 2
    assert admin_client.get(download_url).status_code == 200
    assert admin_client.get(download_url + "?file_version=1").status_code == 409
