import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from documents.models import Document

pytestmark = pytest.mark.django_db


@pytest.fixture
def person(tenant, admin_user):
    from crm.models import Person

    return Person.objects.create(tenant=tenant, surname="Док", given_name="Клиент", created_by=admin_user)


@pytest.fixture
def order(admin_client, person):
    return admin_client.post(
        "/api/v1/orders/",
        {"request_type": "individual", "client_person": str(person.id)},
        format="json",
    ).json()


def upload_file(name="doc.txt", content=b"hello"):
    return SimpleUploadedFile(name, content, content_type="text/plain")


class TestDocuments:
    def test_multipart_upload_creates_version(self, admin_client, order):
        response = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file(),
                "document": json.dumps(
                    {
                        "order": order["id"],
                        "kind": "other",
                        "title": "Документ заказа",
                        "source": "upload",
                    }
                ),
            },
            format="multipart",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["current_version"] == 1
        document = Document.objects.get(pk=body["id"])
        assert document.versions.count() == 1

    def test_new_version_does_not_overwrite_previous(self, admin_client, order):
        created = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file("v1.txt", b"version 1"),
                "document": json.dumps(
                    {"order": order["id"], "kind": "other", "title": "Версионируемый документ"}
                ),
            },
            format="multipart",
        ).json()
        response = admin_client.post(
            f"/api/v1/documents/{created['id']}/versions/",
            {"file": upload_file("v2.txt", b"version 2"), "reason": "исправление"},
            format="multipart",
        )
        assert response.status_code == 201, response.content
        assert response.json()["version"] == 2
        versions = admin_client.get(f"/api/v1/documents/{created['id']}/versions/").json()
        assert [row["version"] for row in versions] == [2, 1]

    def test_service_must_belong_to_document_order(self, admin_client, order, tenant, admin_user):
        from crm.models import Person

        other_person = Person.objects.create(
            tenant=tenant, surname="Другой", given_name="Док", created_by=admin_user
        )
        other_order = admin_client.post(
            "/api/v1/orders/",
            {"request_type": "individual", "client_person": str(other_person.id)},
            format="json",
        ).json()
        service = admin_client.post(
            f"/api/v1/orders/{other_order['id']}/services/",
            {"kind": "hotel", "title": "Чужая услуга", "currency": "USD", "client_total": "100.00"},
            format="json",
        ).json()
        response = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file(),
                "document": json.dumps(
                    {
                        "order": order["id"],
                        "service": service["id"],
                        "kind": "voucher",
                        "title": "Чужой ваучер",
                    }
                ),
            },
            format="multipart",
        )
        assert response.status_code == 400
        assert response.json()["error"]["fields"]["service"]
