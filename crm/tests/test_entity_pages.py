import pytest

from conftest import auth_client, make_user
from crm.models import ClientProfile, Company, Person
from orders.models import Order
from services.models import OrderService
from suppliers.models import Supplier

pytestmark = pytest.mark.django_db


def test_customer_totals_are_live_and_separate_currencies(admin_client, tenant):
    person = Person.objects.create(tenant=tenant, surname='Real', given_name='Client')
    client = ClientProfile.objects.create(tenant=tenant, person=person)
    order = Order.objects.create(tenant=tenant, client_person=person, number='METRIC-1')
    for currency, amount in [('USD', 100), ('KGS', 1000)]:
        OrderService.objects.create(tenant=tenant, order=order, kind='avia', status='issued', currency=currency, client_total=amount)
    row = admin_client.get('/api/v1/clients/').json()['results'][0]
    assert row['metrics']['orders'] == 1
    assert row['metrics']['spent'] == {'USD': '100', 'KGS': '1000'} or {k: float(v) for k,v in row['metrics']['spent'].items()} == {'USD':100,'KGS':1000}
    response = admin_client.patch(f'/api/v1/clients/{client.pk}/', {'status':'vip','person_data':{'city':'Ош'}}, format='json')
    assert response.status_code == 200
    assert response.json()['status'] == 'vip'
    assert response.json()['person_detail']['city'] == 'Ош'


def test_supplier_settings_shared_and_do_not_store_credentials(admin_client, tenant):
    supplier = Supplier.objects.create(tenant=tenant, name='Real supplier')
    path = f'/api/v1/suppliers/{supplier.pk}/settings/'
    response = admin_client.patch(path, {'value':{'api':{'url':'https://supplier.example','password':'must-not-store'},'local':{'contact':'Operator'},'fin':{'currency':'KGS'}}}, format='json')
    assert response.status_code == 200, response.content
    other_admin = auth_client(make_user(tenant, 'second-admin@example.test', 'admin'))
    value = other_admin.get(path).json()['value']
    assert value['local']['contact'] == 'Operator'
    assert 'password' not in value['api']
    supplier.refresh_from_db()
    assert supplier.contact_person == 'Operator'


def test_entity_chats_reuse_thread_and_reject_foreign_person(admin_client, tenant, other_tenant):
    person = Person.objects.create(tenant=tenant, surname='Peer', given_name='Client')
    body = {'type':'client','client_person':str(person.pk),'title':'Peer'}
    first = admin_client.post('/api/v1/chat/threads/', body, format='json')
    second = admin_client.post('/api/v1/chat/threads/', body, format='json')
    assert first.status_code == 201, first.content
    assert second.status_code == 200
    assert first.json()['id'] == second.json()['id']
    person = Person.objects.create(tenant=other_tenant, surname='Private', given_name='Client')
    assert admin_client.post('/api/v1/chat/threads/', {**body,'client_person':str(person.pk)}, format='json').status_code == 404


def test_passenger_group_round_trip_and_version_conflict(admin_client, tenant):
    company = Company.objects.create(tenant=tenant, legal_name='Group Company')
    response = admin_client.post('/api/v1/passenger-groups/', {'company':str(company.pk),'name':'Real group','type':'Делегация','members':[{'name':'Real Member','docNo':'TEST-123'}],'subgroups':[{'id':'team','name':'Team'}]}, format='json')
    assert response.status_code == 201, response.content
    group = response.json()
    path = f"/api/v1/passenger-groups/{group['id']}/"
    assert admin_client.patch(path, {'version':group['version'],'name':'Renamed'}, format='json').status_code == 200
    assert admin_client.patch(path, {'version':group['version'],'name':'Stale'}, format='json').status_code == 409
    rows = admin_client.get(f'/api/v1/passenger-groups/?company={company.pk}').json()['results']
    assert rows[0]['name'] == 'Renamed'
    assert rows[0]['members'][0]['docNo'] == 'TEST-123'
    assert admin_client.delete(path).status_code == 204
    assert admin_client.get('/api/v1/passenger-groups/').json()['results'] == []


def test_client_creation_documents_are_atomic(admin_client, tenant, other_tenant):
    foreign = Person.objects.create(tenant=other_tenant, surname='Foreign', given_name='Person')
    assert admin_client.post('/api/v1/clients/', {'person': str(foreign.pk)}, format='json').status_code == 400
    payload = {'person_data': {'surname': 'Atomic', 'given_name': 'Test'}, 'documents': [{'type': 'foreign_passport'}]}
    assert admin_client.post('/api/v1/clients/', payload, format='json').status_code == 400
    assert not Person.objects.filter(tenant=tenant, surname='Atomic').exists()
    payload['documents'][0]['number'] = 'QA-ATOMIC-01'
    response = admin_client.post('/api/v1/clients/', payload, format='json')
    assert response.status_code == 201, response.content
    docs = admin_client.get(f"/api/v1/persons/{response.json()['person']}/documents/").json()
    assert docs[0]['number_masked'] == 'QA-ATOMIC-01'


def test_roster_import_and_office_exports_use_real_rows(admin_client):
    import io
    from zipfile import ZipFile

    from django.core.files.uploadedfile import SimpleUploadedFile
    from openpyxl import load_workbook

    file = SimpleUploadedFile('roster.csv', 'Фамилия;Имя;Паспорт\nТестов;Пассажир;REAL123'.encode('utf-8'))
    response = admin_client.post('/api/v1/roster-parse/', {'file': file}, format='multipart')
    assert response.status_code == 200, response.content
    assert response.json()['results'][0]['document_number'] == 'REAL123'
    invalid = SimpleUploadedFile('roster.xlsx', b'not a workbook')
    assert admin_client.post('/api/v1/roster-parse/', {'file': invalid}, format='multipart').status_code == 400
    table = {'headers': ['Имя', 'Документ'], 'rows': [['Тестов', '=1+1']]}
    xlsx = admin_client.post('/api/v1/roster-export/', {**table, 'format': 'Excel'}, format='json')
    assert xlsx.status_code == 200, xlsx.content
    sheet = load_workbook(io.BytesIO(xlsx.content)).active
    assert sheet['A2'].value == 'Тестов'
    assert sheet['B2'].data_type == 's'
    docx = admin_client.post('/api/v1/roster-export/', {**table, 'format': 'Word'}, format='json')
    with ZipFile(io.BytesIO(docx.content)) as archive:
        assert 'Тестов' in archive.read('word/document.xml').decode()
    csv = admin_client.post('/api/v1/roster-export/', {**table, 'format': 'CSV', 'encoding': 'Windows-1251'}, format='json')
    assert "'=1+1" in csv.content.decode('cp1251')


def test_group_masks_documents_without_document_permission(tenant):
    import json

    from accounts.models import RolePermission
    from groups_app.models import PassengerGroup

    user = make_user(tenant, 'roster-limited@example.test', 'operator')
    for role in user.user_roles.all():
        RolePermission.objects.filter(role=role.role, permission_code='crm.view_person_documents').delete()
    group = PassengerGroup.objects.create(tenant=tenant, name='Private', roster=json.dumps([{'name':'Test', 'docNo':'SECRET123'}]))
    response = auth_client(user).get('/api/v1/passenger-groups/')
    assert response.status_code == 200
    assert response.json()['results'][0]['members'][0]['docNo'] != 'SECRET123'
    group.refresh_from_db()
    assert 'SECRET123' in group.roster


def test_supplier_avia_markups_save_and_replace_only_managed_rules(admin_client, tenant):
    from suppliers.models import SupplierMarkupRule
    supplier = Supplier.objects.create(tenant=tenant, name='Air QA')
    existing = SupplierMarkupRule.objects.create(tenant=tenant, supplier=supplier, service_kind='hotel', amount_type='fixed', amount_value=5)
    path = f'/api/v1/suppliers/{supplier.pk}/avia-markups/'
    config = {'SU': {'domestic': {'type':'percent', 'value':5}, 'intl': {'type':'fixed', 'value':10}, 'routes':[]}}
    response = admin_client.patch(path, {'value':config}, format='json')
    assert response.status_code == 200, response.content
    assert admin_client.get(path).json()['value'] == config
    assert supplier.markup_rules.filter(archived_at__isnull=True).count() == 3
    assert admin_client.patch(path, {'value':{}}, format='json').status_code == 200
    assert list(supplier.markup_rules.filter(archived_at__isnull=True).values_list('id',flat=True)) == [existing.pk]



def test_airline_markup_respects_geography_and_specific_route(admin_client, tenant):
    from services.pricing import resolve_markup_rules
    supplier = Supplier.objects.create(tenant=tenant, name="Geo")
    config = {"SU": {"domestic": {"type": "fixed", "value": 5}, "intl": {"type": "fixed", "value": 10}, "routes": [{"from": "SVO", "to": "FRU", "type": "fixed", "value": 20}]}}
    assert admin_client.patch(f"/api/v1/suppliers/{supplier.pk}/avia-markups/", {"value": config}, format="json").status_code == 200
    assert resolve_markup_rules(supplier, kind="avia", airline="SU", geography="domestic")[0].amount_value == 5
    assert resolve_markup_rules(supplier, kind="avia", airline="SU", geography="intl")[0].amount_value == 10
    assert resolve_markup_rules(supplier, kind="avia", airline="SU", route="SVO-FRU", geography="intl")[0].amount_value == 20
    assert not resolve_markup_rules(supplier, kind="avia", airline="SU")



def test_fixed_supplier_markup_converts_currency(tenant):
    from decimal import Decimal

    from django.utils import timezone

    from finance.models import ExchangeRate
    from services.pricing import calculate_price
    from suppliers.models import SupplierMarkupRule

    supplier = Supplier.objects.create(tenant=tenant, name="Currency QA")
    rule = SupplierMarkupRule.objects.create(tenant=tenant, supplier=supplier, amount_type="fixed", amount_value=10, currency="USD")
    ExchangeRate.objects.create(tenant=tenant, from_currency="USD", to_currency="KGS", rate=87, as_of=timezone.now())
    result = calculate_price(base=Decimal(1000), currency="KGS", markup_rules=[rule])
    assert Decimal(str(result["total"])) == Decimal(1870)


def test_person_and_employee_document_payload_persists_atomically(admin_client, tenant):
    payload = {'surname': 'Employee', 'given_name': 'Documents', 'secondary_phone': '+996700000001', 'documents': [{'type': 'id_card', 'number': 'STAFF1234'}]}
    response = admin_client.post('/api/v1/persons/', payload, format='json')
    assert response.status_code == 201, response.content
    person = response.json()
    assert person['secondary_phone'] == '+996700000001'
    assert admin_client.get(f"/api/v1/persons/{person['id']}/documents/").json()[0]['number_masked'] == 'STAFF1234'
    invalid = {**payload, 'surname': 'Rollback', 'given_name': 'Invalid', 'secondary_phone': '', 'documents': [{'type': 'id_card'}]}
    assert admin_client.post('/api/v1/persons/', invalid, format='json').status_code == 400
    assert not Person.objects.filter(tenant=tenant, surname='Rollback').exists()
