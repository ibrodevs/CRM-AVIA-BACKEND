"""CRM API (ТЗ §6.4)."""
import hashlib

from django.db.models import Q
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.pagination import DefaultPagination
from crm.models import (
    Agreement, ClientProfile, Company, Contract, Department, Employee, FeeTemplate,
    LoyaltyCard, Person, PersonDocument, SettlementProfile,
)
from crm.serializers import (
    AgreementSerializer, ClientProfileSerializer, CompanySerializer, ContractSerializer,
    DepartmentSerializer, EmployeeSerializer, FeeTemplateSerializer, LoyaltyCardSerializer,
    PersonDocumentSerializer, PersonSerializer, SettlementProfileSerializer,
)


def _tenant_qs(model, request):
    return model.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True)


def find_person_duplicates(request, data: dict) -> list[Person]:
    """Вероятные дубли: нормализованные ФИО+ДР, телефон/email, документ (ТЗ §6.4)."""
    qs = _tenant_qs(Person, request)
    conditions = Q()
    surname = str(data.get("surname", "")).strip().lower()
    given = str(data.get("given_name", "")).strip().lower()
    if surname and given and data.get("birth_date"):
        conditions |= Q(surname__iexact=surname, given_name__iexact=given,
                        birth_date=data["birth_date"])
    if phone := str(data.get("phone", "")).strip():
        conditions |= Q(phone=phone)
    if email := str(data.get("email", "")).strip():
        conditions |= Q(email__iexact=email)
    if not conditions:
        return []
    return list(qs.filter(conditions)[:10])


class PersonListCreateView(GenericAPIView):
    permission_classes = [require("crm.view")]
    pagination_class = DefaultPagination
    serializer_class = PersonSerializer

    def get(self, request):
        qs = _tenant_qs(Person, request).order_by("surname", "given_name")
        params = request.query_params
        if q := params.get("q", "").strip():
            qs = qs.filter(
                Q(surname__icontains=q) | Q(given_name__icontains=q)
                | Q(latin_surname__icontains=q) | Q(latin_given_name__icontains=q)
                | Q(phone__icontains=q) | Q(email__icontains=q)
            )
        if city := params.get("city"):
            qs = qs.filter(city__iexact=city)
        if person_status := params.get("status"):
            qs = qs.filter(status=person_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(PersonSerializer(page, many=True).data)

    def post(self, request):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        serializer = PersonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        duplicates = find_person_duplicates(request, serializer.validated_data)
        if duplicates and not request.data.get("force_create"):
            raise ApiError(
                code="POSSIBLE_DUPLICATE",
                message="Найдены вероятные дубли лица",
                details={"candidates": PersonSerializer(duplicates, many=True).data,
                         "hint": "Передайте force_create=true и reason (требуется право)"},
                status_code=409,
            )
        if duplicates and request.data.get("force_create"):
            if not has_permission(request.user, "crm.force_create_duplicate"):
                raise ApiError(code="PERMISSION_DENIED",
                               message="Нет права создавать при возможном дубле",
                               status_code=403)
            if not request.data.get("reason"):
                raise ApiError(code="REASON_REQUIRED",
                               message="Принудительное создание требует причины",
                               status_code=400)

        person = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        audit("crm.person_created", actor=request.user, resource=person, request=request,
              reason=str(request.data.get("reason", "")))
        return Response(PersonSerializer(person).data, status=http.HTTP_201_CREATED)


class PersonDetailView(APIView):
    permission_classes = [require("crm.view")]

    def _get(self, request, person_id) -> Person:
        person = _tenant_qs(Person, request).filter(pk=person_id).first()
        if person is None:
            raise ApiError(code="NOT_FOUND", message="Лицо не найдено", status_code=404)
        return person

    def get(self, request, person_id):
        return Response(PersonSerializer(self._get(request, person_id)).data)

    def patch(self, request, person_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        person = self._get(request, person_id)
        serializer = PersonSerializer(person, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user, version=person.version + 1)
        audit("crm.person_updated", actor=request.user, resource=person, request=request)
        return Response(serializer.data)


class PersonDocumentsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, person_id):
        documents = PersonDocument.objects.filter(
            person_id=person_id, tenant_id=request.user.tenant_id, archived_at__isnull=True
        )
        return Response(PersonDocumentSerializer(documents, many=True,
                                                 context={"request": request}).data)

    def post(self, request, person_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        person = _tenant_qs(Person, request).filter(pk=person_id).first()
        if person is None:
            raise ApiError(code="NOT_FOUND", message="Лицо не найдено", status_code=404)
        serializer = PersonDocumentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        # дубль документа по нормализованному номеру (ТЗ §30)
        normalized = str(serializer.validated_data["number"]).replace(" ", "").upper()
        number_hash = hashlib.sha256(normalized.encode()).hexdigest()
        existing = PersonDocument.objects.filter(
            tenant_id=request.user.tenant_id,
            type=serializer.validated_data["type"],
            issuing_country=serializer.validated_data.get("issuing_country", ""),
            number_hash=number_hash, archived_at__isnull=True,
        ).first()
        if existing is not None:
            raise ApiError(code="DUPLICATE_DOCUMENT",
                           message="Документ с таким номером уже зарегистрирован",
                           details={"person_id": str(existing.person_id)}, status_code=409)
        document = serializer.save(tenant_id=request.user.tenant_id, person=person,
                                   created_by=request.user)
        audit("crm.person_document_added", actor=request.user, resource=person,
              request=request, after={"type": document.type})
        return Response(PersonDocumentSerializer(document, context={"request": request}).data,
                        status=http.HTTP_201_CREATED)


class PersonLoyaltyCardsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, person_id):
        cards = LoyaltyCard.objects.filter(person_id=person_id,
                                           tenant_id=request.user.tenant_id,
                                           archived_at__isnull=True)
        return Response(LoyaltyCardSerializer(cards, many=True).data)

    def post(self, request, person_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        person = _tenant_qs(Person, request).filter(pk=person_id).first()
        if person is None:
            raise ApiError(code="NOT_FOUND", message="Лицо не найдено", status_code=404)
        serializer = LoyaltyCardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        card = serializer.save(tenant_id=request.user.tenant_id, person=person,
                               created_by=request.user)
        return Response(LoyaltyCardSerializer(card).data, status=http.HTTP_201_CREATED)


class ClientListCreateView(GenericAPIView):
    permission_classes = [require("crm.view")]
    pagination_class = DefaultPagination
    serializer_class = ClientProfileSerializer

    def get(self, request):
        qs = _tenant_qs(ClientProfile, request).select_related("person")
        params = request.query_params
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(person__surname__icontains=q) | Q(person__given_name__icontains=q)
                           | Q(person__phone__icontains=q) | Q(person__email__icontains=q))
        if client_status := params.get("status"):
            qs = qs.filter(status=client_status)
        if manager := params.get("manager"):
            qs = qs.filter(assigned_manager_id=manager)
        page = self.paginate_queryset(qs.order_by("person__surname"))
        return self.get_paginated_response(ClientProfileSerializer(page, many=True).data)

    def post(self, request):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        serializer = ClientProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        return Response(ClientProfileSerializer(profile).data, status=http.HTTP_201_CREATED)


class CompanyListCreateView(GenericAPIView):
    permission_classes = [require("crm.view")]
    pagination_class = DefaultPagination
    serializer_class = CompanySerializer

    def get(self, request):
        qs = _tenant_qs(Company, request).order_by("legal_name")
        params = request.query_params
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(legal_name__icontains=q) | Q(short_name__icontains=q)
                           | Q(tax_id__icontains=q))
        if company_status := params.get("status"):
            qs = qs.filter(status=company_status)
        if manager := params.get("manager"):
            qs = qs.filter(assigned_manager_id=manager)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(CompanySerializer(page, many=True).data)

    def post(self, request):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        serializer = CompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if tax_id := serializer.validated_data.get("tax_id"):
            existing = _tenant_qs(Company, request).filter(tax_id=tax_id).first()
            if existing is not None:
                raise ApiError(code="DUPLICATE_TAX_ID",
                               message="Компания с таким ИНН уже существует",
                               details={"company_id": str(existing.id)}, status_code=409)
        company = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        audit("crm.company_created", actor=request.user, resource=company, request=request)
        return Response(CompanySerializer(company).data, status=http.HTTP_201_CREATED)


def _get_company(request, company_id) -> Company:
    company = _tenant_qs(Company, request).filter(pk=company_id).first()
    if company is None:
        raise ApiError(code="NOT_FOUND", message="Компания не найдена", status_code=404)
    return company


class CompanyDetailView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        return Response(CompanySerializer(_get_company(request, company_id)).data)

    def patch(self, request, company_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        company = _get_company(request, company_id)
        serializer = CompanySerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user, version=company.version + 1)
        audit("crm.company_updated", actor=request.user, resource=company, request=request)
        return Response(serializer.data)


class CompanyEmployeesView(GenericAPIView):
    permission_classes = [require("crm.view")]
    pagination_class = DefaultPagination

    def get(self, request, company_id):
        company = _get_company(request, company_id)
        qs = company.employees.filter(archived_at__isnull=True).select_related("person")
        if q := request.query_params.get("q", "").strip():
            qs = qs.filter(Q(person__surname__icontains=q) | Q(person__given_name__icontains=q))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(EmployeeSerializer(page, many=True).data)

    def post(self, request, company_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        company = _get_company(request, company_id)
        serializer = EmployeeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        employee = serializer.save(tenant_id=request.user.tenant_id, company=company,
                                   created_by=request.user)
        return Response(EmployeeSerializer(employee).data, status=http.HTTP_201_CREATED)


class CompanyDepartmentsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        company = _get_company(request, company_id)
        departments = company.departments.filter(archived_at__isnull=True)
        return Response(DepartmentSerializer(departments, many=True).data)

    def post(self, request, company_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        company = _get_company(request, company_id)
        serializer = DepartmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = serializer.save(tenant_id=request.user.tenant_id, company=company,
                                     created_by=request.user)
        return Response(DepartmentSerializer(department).data, status=http.HTTP_201_CREATED)


class CompanyContractsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        company = _get_company(request, company_id)
        contracts = company.contracts.filter(archived_at__isnull=True).prefetch_related(
            "agreements__fee_rules"
        )
        return Response(ContractSerializer(contracts, many=True).data)

    def post(self, request, company_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        company = _get_company(request, company_id)
        serializer = ContractSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contract = serializer.save(tenant_id=request.user.tenant_id, company=company,
                                   created_by=request.user)
        return Response(ContractSerializer(contract).data, status=http.HTTP_201_CREATED)


class ContractAgreementsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, contract_id):
        contract = _tenant_qs(Contract, request).filter(pk=contract_id).first()
        if contract is None:
            raise ApiError(code="NOT_FOUND", message="Договор не найден", status_code=404)
        return Response(AgreementSerializer(contract.agreements.all(), many=True).data)

    def post(self, request, contract_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        contract = _tenant_qs(Contract, request).filter(pk=contract_id).first()
        if contract is None:
            raise ApiError(code="NOT_FOUND", message="Договор не найден", status_code=404)
        serializer = AgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        last = contract.agreements.order_by("-agreement_version").first()
        agreement = serializer.save(
            tenant_id=request.user.tenant_id, contract=contract,
            agreement_version=(last.agreement_version + 1) if last else 1,
            created_by=request.user,
        )
        return Response(AgreementSerializer(agreement).data, status=http.HTTP_201_CREATED)


class CompanySettlementView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        company = _get_company(request, company_id)
        profile, _ = SettlementProfile.objects.get_or_create(
            company=company, defaults={"tenant_id": company.tenant_id}
        )
        return Response(SettlementProfileSerializer(profile).data)

    def patch(self, request, company_id):
        if not has_permission(request.user, "finance.view"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права finance.view",
                           status_code=403)
        company = _get_company(request, company_id)
        profile, _ = SettlementProfile.objects.get_or_create(
            company=company, defaults={"tenant_id": company.tenant_id}
        )
        serializer = SettlementProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        audit("crm.settlement_updated", actor=request.user, resource=company, request=request)
        return Response(serializer.data)


class FeeTemplateListCreateView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request):
        templates = _tenant_qs(FeeTemplate, request).prefetch_related("rules")
        return Response(FeeTemplateSerializer(templates, many=True).data)

    def post(self, request):
        if not has_permission(request.user, "settings.manage"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права settings.manage",
                           status_code=403)
        serializer = FeeTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        from crm.models import FeeRule
        from crm.serializers import FeeRuleSerializer

        for rule in request.data.get("rules", []):
            rule_serializer = FeeRuleSerializer(data=rule)
            rule_serializer.is_valid(raise_exception=True)
            FeeRule.objects.create(tenant_id=request.user.tenant_id, template=template,
                                   created_by=request.user, **rule_serializer.validated_data)
        template.refresh_from_db()
        return Response(FeeTemplateSerializer(template).data, status=http.HTTP_201_CREATED)
