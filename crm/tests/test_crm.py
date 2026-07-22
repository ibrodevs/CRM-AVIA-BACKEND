import pytest

from conftest import auth_client

pytestmark = pytest.mark.django_db

PERSON = {
    "surname": "Иванов",
    "given_name": "Пётр",
    "birth_date": "1990-05-10",
    "phone": "+996700000001",
    "email": "ivanov@test.local",
}


class TestPersonDuplicates:
    def test_create_ok(self, admin_client):
        response = admin_client.post("/api/v1/persons/", PERSON, format="json")
        assert response.status_code == 201

    def test_duplicate_detected(self, admin_client):
        admin_client.post("/api/v1/persons/", PERSON, format="json")
        response = admin_client.post("/api/v1/persons/", PERSON, format="json")
        assert response.status_code == 409
        body = response.json()["error"]
        assert body["code"] == "POSSIBLE_DUPLICATE"
        assert len(body["details"]["candidates"]) == 1

    def test_force_create_requires_permission_and_reason(self, admin_client, operator_user):
        admin_client.post("/api/v1/persons/", PERSON, format="json")

        response = admin_client.post("/api/v1/persons/", {**PERSON, "force_create": True}, format="json")
        assert response.status_code == 400

        response = admin_client.post(
            "/api/v1/persons/",
            {**PERSON, "force_create": True, "reason": "тёзка"},
            format="json",
        )
        assert response.status_code == 201

        operator = auth_client(operator_user)
        response = operator.post(
            "/api/v1/persons/",
            {**PERSON, "force_create": True, "reason": "тёзка"},
            format="json",
        )
        assert response.status_code == 403

    def test_phone_only_duplicate(self, admin_client):
        admin_client.post("/api/v1/persons/", PERSON, format="json")
        response = admin_client.post(
            "/api/v1/persons/",
            {"surname": "Петров", "given_name": "Иван", "phone": PERSON["phone"]},
            format="json",
        )
        assert response.status_code == 409


class TestPersonDocuments:
    def _person(self, client) -> str:
        return client.post("/api/v1/persons/", PERSON, format="json").json()["id"]

    DOC = {
        "type": "foreign_passport",
        "number": "AC1234567",
        "issuing_country": "KG",
        "expires_at": "2030-01-01",
    }

    def test_document_encrypted_and_masked(self, admin_client, operator_user, tenant):
        person_id = self._person(admin_client)
        response = admin_client.post(f"/api/v1/persons/{person_id}/documents/", self.DOC, format="json")
        assert response.status_code == 201

        assert response.json()["number_masked"] == "AC1234567"

        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT number FROM crm_person_document")
            raw = cursor.fetchone()[0]
        assert "AC1234567" not in raw
        assert raw.startswith("enc$1$")

        operator = auth_client(operator_user)
        docs = operator.get(f"/api/v1/persons/{person_id}/documents/").json()
        assert docs[0]["number_masked"] == "*****4567"

    def test_duplicate_document_number(self, admin_client):
        first = self._person(admin_client)
        admin_client.post(f"/api/v1/persons/{first}/documents/", self.DOC, format="json")
        second = admin_client.post(
            "/api/v1/persons/",
            {"surname": "Сидоров", "given_name": "Олег"},
            format="json",
        ).json()["id"]
        response = admin_client.post(f"/api/v1/persons/{second}/documents/", self.DOC, format="json")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_DOCUMENT"


class TestClients:
    def test_create_client_with_person_atomically(self, admin_client):
        response = admin_client.post(
            "/api/v1/clients/",
            {"client_type": "individual", "status": "active", "person_data": PERSON},
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["person_detail"]["email"] == PERSON["email"]

    def test_nested_duplicate_does_not_create_profile(self, admin_client):
        admin_client.post("/api/v1/persons/", PERSON, format="json")
        response = admin_client.post(
            "/api/v1/clients/",
            {"client_type": "individual", "person_data": PERSON},
            format="json",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "POSSIBLE_DUPLICATE"


class TestCompanies:
    COMPANY = {"legal_name": "ОсОО Ромашка", "tax_id": "01234567890123", "bank_account": "KG12345678901234"}

    def test_list_companies_uses_company_fields_for_sorting(self, admin_client):
        admin_client.post("/api/v1/companies/", self.COMPANY, format="json")

        response = admin_client.get("/api/v1/companies/")

        assert response.status_code == 200, response.content
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["legal_name"] == self.COMPANY["legal_name"]

    def test_create_and_mask_bank_account(self, admin_client):
        response = admin_client.post("/api/v1/companies/", self.COMPANY, format="json")
        assert response.status_code == 201
        body = response.json()
        assert body["bank_account_masked"].endswith("1234")
        assert "bank_account" not in body or body.get("bank_account") is None

    def test_tax_id_unique(self, admin_client):
        admin_client.post("/api/v1/companies/", self.COMPANY, format="json")
        response = admin_client.post("/api/v1/companies/", self.COMPANY, format="json")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_TAX_ID"

    def test_company_staff_create_move_and_archive(self, admin_client):
        company = admin_client.post("/api/v1/companies/", self.COMPANY, format="json").json()
        sales = admin_client.post(
            f"/api/v1/companies/{company['id']}/departments/",
            {"name": "Продажи"},
            format="json",
        )
        assert sales.status_code == 201, sales.content
        ops = admin_client.post(
            f"/api/v1/companies/{company['id']}/departments/",
            {"name": "Операторы"},
            format="json",
        ).json()
        person = admin_client.post(
            "/api/v1/persons/",
            {"surname": "Сотрудник", "given_name": "Первый", "email": "emp@test.local"},
            format="json",
        ).json()
        employee = admin_client.post(
            f"/api/v1/companies/{company['id']}/employees/",
            {"person": person["id"], "department": sales.json()["id"], "position": "Менеджер"},
            format="json",
        )
        assert employee.status_code == 201, employee.content

        moved = admin_client.patch(
            f"/api/v1/companies/{company['id']}/employees/{employee.json()['id']}/",
            {"department": ops["id"], "position": "Старший менеджер"},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        assert moved.json()["department"] == ops["id"]
        assert moved.json()["position"] == "Старший менеджер"

        response = admin_client.delete(
            f"/api/v1/companies/{company['id']}/employees/{employee.json()['id']}/",
            format="json",
        )
        assert response.status_code == 204
        employees = admin_client.get(f"/api/v1/companies/{company['id']}/employees/").json()
        assert employees["count"] == 0

    def test_employee_department_must_belong_to_company(self, admin_client):
        first = admin_client.post("/api/v1/companies/", self.COMPANY, format="json").json()
        second = admin_client.post(
            "/api/v1/companies/",
            {"legal_name": "ОсОО Другая", "tax_id": "99999999999999"},
            format="json",
        ).json()
        department = admin_client.post(
            f"/api/v1/companies/{second['id']}/departments/",
            {"name": "Чужой отдел"},
            format="json",
        ).json()
        person = admin_client.post(
            "/api/v1/persons/",
            {"surname": "Сотрудник", "given_name": "Второй", "email": "emp2@test.local"},
            format="json",
        ).json()

        response = admin_client.post(
            f"/api/v1/companies/{first['id']}/employees/",
            {"person": person["id"], "department": department["id"], "position": "Менеджер"},
            format="json",
        )

        assert response.status_code == 400
        assert "department" in response.json()["error"]["fields"]

    def test_department_with_employees_cannot_be_deleted(self, admin_client):
        company = admin_client.post("/api/v1/companies/", self.COMPANY, format="json").json()
        department = admin_client.post(
            f"/api/v1/companies/{company['id']}/departments/",
            {"name": "Продажи"},
            format="json",
        ).json()
        person = admin_client.post(
            "/api/v1/persons/",
            {"surname": "Сотрудник", "given_name": "Третий", "email": "emp3@test.local"},
            format="json",
        ).json()
        admin_client.post(
            f"/api/v1/companies/{company['id']}/employees/",
            {"person": person["id"], "department": department["id"], "position": "Менеджер"},
            format="json",
        )

        response = admin_client.delete(
            f"/api/v1/companies/{company['id']}/departments/{department['id']}/",
            format="json",
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DEPARTMENT_HAS_EMPLOYEES"


class TestTravelPolicy:
    def test_policy_check(self, admin_client):
        company = admin_client.post("/api/v1/companies/", {"legal_name": "ОсОО Тест"}, format="json").json()
        policy = admin_client.post(
            f"/api/v1/companies/{company['id']}/travel-policies/",
            {
                "name": "Базовая",
                "allowed_avia_cabins": ["economy"],
                "price_limits": {"avia": {"amount": "500", "currency": "USD"}},
            },
            format="json",
        ).json()

        response = admin_client.post(
            f"/api/v1/travel-policies/{policy['id']}/check/",
            {"offer": {"kind": "avia", "cabin": "economy", "price": {"amount": "300", "currency": "USD"}}},
            format="json",
        )
        assert response.json()["verdict"] == "allowed"

        response = admin_client.post(
            f"/api/v1/travel-policies/{policy['id']}/check/",
            {"offer": {"kind": "avia", "cabin": "business", "price": {"amount": "900", "currency": "USD"}}},
            format="json",
        )
        body = response.json()
        assert body["verdict"] == "approval_required"
        assert len(body["violations"]) == 2
