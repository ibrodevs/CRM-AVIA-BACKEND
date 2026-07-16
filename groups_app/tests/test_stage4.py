import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from crm.models import Person

pytestmark = pytest.mark.django_db


@pytest.fixture
def group_order_setup(admin_client, tenant, admin_user):
    persons = [
        Person.objects.create(tenant=tenant, surname=f"Пассажир{i}", given_name="Тест",
                              latin_surname=f"PASSAZHIR{i}", latin_given_name="TEST",
                              birth_date="1990-01-01", gender="male", citizenship="KG",
                              created_by=admin_user)
        for i in range(3)
    ]
    order = admin_client.post("/api/v1/orders/", {
        "request_type": "group", "client_person": str(persons[0].id),
        "participants": [{"person": str(p.id)} for p in persons],
    }, format="json").json()
    group_order = admin_client.post("/api/v1/group-orders/", {
        "order": order["id"], "scenario": "classic_block", "requested_seats": 10,
    }, format="json").json()
    block = admin_client.post(f"/api/v1/group-orders/{group_order['id']}/blocks/", {
        "name": "Блок A", "seats": 2,
    }, format="json").json()
    return order, group_order, block, persons


class TestGroups:
    def test_mass_assign_partial_success(self, admin_client, group_order_setup):
        order, group_order, block, persons = group_order_setup
        participants = admin_client.get(
            f"/api/v1/orders/{order['id']}/participants/").json()
        items = [{"participant_id": p["id"], "block_id": block["id"]}
                 for p in participants]
        response = admin_client.post(
            f"/api/v1/group-orders/{group_order['id']}/mass-actions/",
            {"action": "assign_block", "items": items}, format="json")
        body = response.json()
        # блок на 2 места, пассажиров 3: 2 ok + 1 BLOCK_FULL
        assert body["summary"] == {"ok": 2, "failed": 1}
        failed = [r for r in body["results"] if r["status"] == "error"]
        assert failed[0]["code"] == "BLOCK_FULL"

    def test_matrix(self, admin_client, group_order_setup):
        order, group_order, block, _ = group_order_setup
        matrix = admin_client.get(
            f"/api/v1/group-orders/{group_order['id']}/matrix/").json()
        assert len(matrix["rows"]) == 3
        assert len(matrix["rows"][0]["cells"]) == 1

    def test_group_status_machine(self, admin_client, group_order_setup):
        _, group_order, _, _ = group_order_setup
        response = admin_client.post(
            f"/api/v1/group-orders/{group_order['id']}/transition/",
            {"target_status": "ticketed"}, format="json")
        assert response.status_code == 409
        response = admin_client.post(
            f"/api/v1/group-orders/{group_order['id']}/transition/",
            {"target_status": "request_sent"}, format="json")
        assert response.status_code == 200


CSV_CONTENT = (
    "Фамилия,Имя,Дата рождения,Пол,Гражданство,Паспорт,Срок действия\n"
    "Пассажир0,Тест,01.01.1990,м,KG,AC1111111,01.01.2030\n"
    "Новый,Человек,05.05.1985,ж,KZ,N2222222,01.01.2031\n"
    "Битая,Строка,,, ,,\n"
)


class TestRosterImport:
    def test_import_preview_apply(self, admin_client, group_order_setup):
        order, _, _, _ = group_order_setup
        file = SimpleUploadedFile("roster.csv", CSV_CONTENT.encode("utf-8"),
                                  content_type="text/csv")
        response = admin_client.post("/api/v1/roster-imports/",
                                     {"order": order["id"], "file": file},
                                     format="multipart")
        assert response.status_code == 201, response.content
        import_id = response.json()["id"]

        preview = admin_client.post(f"/api/v1/roster-imports/{import_id}/preview/").json()
        states = {i["row_index"]: i["state"] for i in preview["items"]}
        assert states[0] == "same"      # существующий пассажир
        assert states[1] == "new"       # новый
        assert preview["stats"]["invalid"] >= 1  # битая строка
        assert preview["stats"]["missing"] == 2  # 2 участника не в файле

        response = admin_client.post(
            f"/api/v1/roster-imports/{import_id}/apply/",
            {"decisions": {"1": "add", "0": "keep_current", "2": "ignore"}},
            format="json")
        assert response.status_code == 200
        participants = admin_client.get(
            f"/api/v1/orders/{order['id']}/participants/").json()
        assert len(participants) == 4  # добавился 1 новый
        # исходные данные не перезаписаны
        from groups_app.models import RosterImportJob

        job = RosterImportJob.objects.get(pk=import_id)
        assert job.raw_rows[0]["фамилия"] == "Пассажир0"

    def test_export_translit(self, admin_client, group_order_setup):
        order, _, _, _ = group_order_setup
        file = SimpleUploadedFile("roster.csv", CSV_CONTENT.encode("utf-8"),
                                  content_type="text/csv")
        import_id = admin_client.post("/api/v1/roster-imports/",
                                      {"order": order["id"], "file": file},
                                      format="multipart").json()["id"]
        response = admin_client.get(
            f"/api/v1/roster-imports/{import_id}/export/?format=csv")
        content = response.content.decode("utf-8")
        assert "PASSAZHIR0" in content or "PASSAZHIR" in content.upper()


class TestDocuments:
    PDF = b"%PDF-1.4 fake pdf content"

    def _create(self, client) -> dict:
        import json

        file = SimpleUploadedFile("ticket.pdf", self.PDF, content_type="application/pdf")
        response = client.post("/api/v1/documents/", {
            "file": file,
            "document": json.dumps({"kind": "ticket", "title": "Билет FRU-IST"}),
        }, format="multipart")
        assert response.status_code == 201, response.content
        return response.json()

    def test_upload_and_download(self, admin_client):
        document = self._create(admin_client)
        assert document["current_version"] == 1
        response = admin_client.get(f"/api/v1/documents/{document['id']}/download/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == self.PDF
        assert "attachment" in response["Content-Disposition"]

    def test_mime_spoofing_rejected(self, admin_client):
        import json

        file = SimpleUploadedFile("fake.pdf", b"<html>not a pdf</html>",
                                  content_type="application/pdf")
        response = admin_client.post("/api/v1/documents/", {
            "file": file, "document": json.dumps({"kind": "other", "title": "x"}),
        }, format="multipart")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "FILE_SIGNATURE_MISMATCH"

    def test_correction_version(self, admin_client):
        document = self._create(admin_client)
        file = SimpleUploadedFile("fixed.pdf", b"%PDF-1.4 fixed",
                                  content_type="application/pdf")
        # без причины — отказ
        response = admin_client.post(f"/api/v1/documents/{document['id']}/versions/",
                                     {"file": file}, format="multipart")
        assert response.status_code == 400
        file = SimpleUploadedFile("fixed.pdf", b"%PDF-1.4 fixed",
                                  content_type="application/pdf")
        response = admin_client.post(
            f"/api/v1/documents/{document['id']}/versions/",
            {"file": file, "reason": "опечатка в фамилии"}, format="multipart")
        assert response.status_code == 201
        assert response.json()["version"] == 2

    def test_confidential_hidden_without_permission(self, admin_client, operator_user):
        import json

        from conftest import auth_client

        file = SimpleUploadedFile("passport.pdf", self.PDF,
                                  content_type="application/pdf")
        document = admin_client.post("/api/v1/documents/", {
            "file": file,
            "document": json.dumps({"kind": "passport", "title": "Паспорт",
                                    "is_confidential": True}),
        }, format="multipart").json()
        operator = auth_client(operator_user)
        response = operator.get(f"/api/v1/documents/{document['id']}/download/")
        assert response.status_code == 404


class TestChat:
    def test_thread_send_read_unread(self, admin_client, operator_user, admin_user):
        from conftest import auth_client

        thread = admin_client.post("/api/v1/chat/threads/", {
            "type": "internal", "title": "Обсуждение заказа",
        }, format="json").json()
        # добавляем оператора
        admin_client.post(f"/api/v1/chat/threads/{thread['id']}/participants/",
                          {"user": str(operator_user.id)}, format="json")
        message = admin_client.post(f"/api/v1/chat/threads/{thread['id']}/send/",
                                    {"body": "Привет, @operator@test.local"},
                                    format="json").json()
        assert message["body"].startswith("Привет")
        operator = auth_client(operator_user)
        assert operator.get("/api/v1/chat/unread-count/").json()["unread"] == 1
        operator.post(f"/api/v1/chat/threads/{thread['id']}/read/", {}, format="json")
        assert operator.get("/api/v1/chat/unread-count/").json()["unread"] == 0

    def test_messages_cursor(self, admin_client):
        thread = admin_client.post("/api/v1/chat/threads/",
                                   {"type": "internal", "title": "т"},
                                   format="json").json()
        for i in range(5):
            admin_client.post(f"/api/v1/chat/threads/{thread['id']}/send/",
                              {"body": f"msg {i}"}, format="json")
        page = admin_client.get(
            f"/api/v1/chat/threads/{thread['id']}/messages/?limit=3").json()
        assert len(page["results"]) == 3
        assert page["has_more"] is True

    def test_webhook_dedup(self, db, tenant):
        from rest_framework.test import APIClient

        client = APIClient()
        payload = {"event_id": "evt-1", "account": "acc-1",
                   "message": {"id": "m1", "text": "hello"}, "sender": "+996700"}
        first = client.post("/api/v1/communications/webhooks/telegram/", payload,
                            format="json")
        assert first.status_code == 200
        second = client.post("/api/v1/communications/webhooks/telegram/", payload,
                             format="json")
        assert second.json()["status"] == "duplicate"
