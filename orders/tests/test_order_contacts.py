from types import SimpleNamespace

import pytest

from booking.job_handlers import _booking_contact
from crm.models import Company, Person
from orders.contacts import contact_person_name, normalized_contact_text, resolve_order_contact
from orders.models import Order, OrderParticipant

pytestmark = pytest.mark.django_db


@pytest.fixture
def corporate_order(tenant):
    company = Company.objects.create(tenant=tenant, legal_name="Contact test company", tax_id="contact-test")
    return Order.objects.create(
        tenant=tenant, number="ORD-CONTACT", client_company=company, request_type="corporate"
    )


@pytest.mark.parametrize(
    "person,expected",
    [
        ({"first_name": "  Anna ", "last_name": " Ivanova "}, "Ivanova Anna"),
        ({"given_name": "  Анна  Мария ", "surname": " Иванова "}, "Иванова Анна Мария"),
        ({"given_name": "Anna", "surname": None}, "Anna"),
        ({"latin_given_name": "ANNA", "latin_surname": "IVANOVA"}, "IVANOVA ANNA"),
        ({"given_name": None, "surname": None}, ""),
        ({"given_name": " \t\n ", "surname": " "}, ""),
    ],
)
def test_normalizes_real_person_names_without_requiring_a_duplicate_string(person, expected):
    assert contact_person_name(person) == expected
    assert normalized_contact_text(None) == ""


def test_selected_person_is_source_of_truth_when_legacy_contact_name_is_empty(corporate_order, tenant):
    person = Person.objects.create(tenant=tenant, surname=" Иванова ", given_name=" Анна ")
    corporate_order.contact_person = person
    corporate_order.contact_name = ""
    contact = resolve_order_contact(corporate_order)
    assert contact["name"] == "Иванова Анна" and contact["valid"]
    assert _booking_contact(corporate_order)["name"] == contact["name"]


def test_old_order_marked_contact_is_reused_without_mutating_order_or_retyping_name(corporate_order, tenant):
    person = Person.objects.create(tenant=tenant, surname="Ivanova", given_name="Anna")
    participant = OrderParticipant.objects.create(
        tenant=tenant, order=corporate_order, person=person, is_contact=True
    )
    contact = resolve_order_contact(corporate_order)
    assert contact["person"] == str(person.pk)
    assert contact["participant"] == str(participant.pk)
    assert contact["valid"] and contact["source"] == "participant"
    corporate_order.refresh_from_db()
    assert corporate_order.contact_person_id is None


def test_missing_contact_blocks_even_when_first_passenger_has_a_name(corporate_order, tenant):
    person = Person.objects.create(tenant=tenant, surname="Иванова", given_name="Анна")
    OrderParticipant.objects.create(tenant=tenant, order=corporate_order, person=person, is_contact=False)
    contact = resolve_order_contact(corporate_order)
    assert not contact["valid"] and not contact["selected"]
    assert contact["message"] == "Укажите контактное лицо заказа."


@pytest.mark.parametrize("name", ["", "  \t\n "])
def test_selected_unnamed_contact_does_not_silently_fall_back_to_another_person(
    corporate_order, tenant, name
):
    person = Person.objects.create(tenant=tenant, surname=name, given_name=name)
    corporate_order.contact_person = person
    corporate_order.contact_name = "Another person"
    contact = resolve_order_contact(corporate_order)
    assert contact["selected"] and not contact["valid"]
    assert contact["message"] == "У контактного лица не заполнено имя."


def test_selected_guest_snapshot_and_legacy_contact_name_remain_supported(corporate_order, tenant):
    OrderParticipant.objects.create(
        tenant=tenant, order=corporate_order, guest_snapshot={"name": " Анна  Иванова "}, is_contact=True
    )
    assert _booking_contact(corporate_order)["name"] == "Анна Иванова"
    legacy = SimpleNamespace(contact_person=None, client_person=None, contact_name=" Anna  Ivanova ")
    assert resolve_order_contact(legacy)["name"] == "Anna Ivanova"
    legacy.contact_name = "   "
    assert not resolve_order_contact(legacy)["valid"]


def test_removed_contact_and_multiple_active_contacts_are_not_arbitrarily_selected(corporate_order, tenant):
    person = Person.objects.create(tenant=tenant, surname="Иванова", given_name="Анна")
    OrderParticipant.objects.create(
        tenant=tenant, order=corporate_order, person=person, is_contact=True, status="removed"
    )
    assert not resolve_order_contact(corporate_order)["selected"]
    OrderParticipant.objects.create(tenant=tenant, order=corporate_order, person=person, is_contact=True)
    other = Person.objects.create(tenant=tenant, surname="Сидоров", given_name="Иван")
    OrderParticipant.objects.create(tenant=tenant, order=corporate_order, person=other, is_contact=True)
    assert resolve_order_contact(corporate_order)["source"] == "ambiguous"


def test_detail_patch_and_overview_use_same_person_and_enforce_tenant_scope(
    admin_client, corporate_order, tenant, other_tenant
):
    person = Person.objects.create(tenant=tenant, surname="Ivanova", given_name="Anna")
    url = f"/api/v1/orders/{corporate_order.pk}/"
    response = admin_client.patch(
        url, {"contact_person": str(person.pk), "version": corporate_order.version}, format="json"
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["contact_person"] == str(person.pk)
    assert data["booking_contact"]["name"] == "Ivanova Anna"
    assert data["booking_contact"]["person"] == data["contact_person"]
    overview = admin_client.get(f"{url}overview/").json()
    assert overview["order"]["booking_contact"] == data["booking_contact"]
    foreign = Person.objects.create(tenant=other_tenant, surname="Other", given_name="Tenant")
    response = admin_client.patch(
        url, {"contact_person": str(foreign.pk), "version": data["version"]}, format="json"
    )
    assert response.status_code == 400
    corporate_order.refresh_from_db()
    assert corporate_order.contact_person_id == person.pk


def test_individual_client_is_still_the_default_contact(corporate_order, tenant):
    person = Person.objects.create(tenant=tenant, surname="", given_name="Анна")
    corporate_order.client_person = person
    assert _booking_contact(corporate_order)["name"] == "Анна"
