from django.urls import path

from crm import views as v
from crm.company_finance_safe_view import CompanyFinancialConditionsView
from crm.fee_resolution_views import ServiceFeeResolveView
from crm.person_document_ocr_view import PersonDocumentRecognizeView

urlpatterns = [
    path("persons/", v.PersonListCreateView.as_view(), name="person-list"),
    path("persons/<uuid:person_id>/", v.PersonDetailView.as_view(), name="person-detail"),
    path("persons/<uuid:person_id>/documents/", v.PersonDocumentsView.as_view(), name="person-documents"),
    path(
        "persons/<uuid:person_id>/loyalty-cards/",
        v.PersonLoyaltyCardsView.as_view(),
        name="person-loyalty-cards",
    ),
    path(
        "person-documents/recognize/",
        PersonDocumentRecognizeView.as_view(),
        name="person-document-recognize",
    ),
    path("clients/", v.ClientListCreateView.as_view(), name="client-list"),
    path("companies/", v.CompanyListCreateView.as_view(), name="company-list"),
    path("companies/<uuid:company_id>/", v.CompanyDetailView.as_view(), name="company-detail"),
    path(
        "companies/<uuid:company_id>/financial-conditions/",
        CompanyFinancialConditionsView.as_view(),
        name="company-financial-conditions",
    ),
    path(
        "companies/<uuid:company_id>/employees/", v.CompanyEmployeesView.as_view(), name="company-employees"
    ),
    path(
        "companies/<uuid:company_id>/employees/import/",
        v.CompanyEmployeesImportView.as_view(),
        name="company-employees-import",
    ),
    path(
        "companies/<uuid:company_id>/employees/<uuid:employee_id>/",
        v.CompanyEmployeeDetailView.as_view(),
        name="company-employee-detail",
    ),
    path(
        "companies/<uuid:company_id>/departments/",
        v.CompanyDepartmentsView.as_view(),
        name="company-departments",
    ),
    path(
        "companies/<uuid:company_id>/departments/<uuid:department_id>/",
        v.CompanyDepartmentDetailView.as_view(),
        name="company-department-detail",
    ),
    path(
        "companies/<uuid:company_id>/contracts/", v.CompanyContractsView.as_view(), name="company-contracts"
    ),
    path(
        "contracts/<uuid:contract_id>/agreements/",
        v.ContractAgreementsView.as_view(),
        name="contract-agreements",
    ),
    path(
        "companies/<uuid:company_id>/settlement/",
        v.CompanySettlementView.as_view(),
        name="company-settlement",
    ),
    path("fee-templates/", v.FeeTemplateListCreateView.as_view(), name="fee-templates"),
    path("service-fee/resolve/", ServiceFeeResolveView.as_view(), name="service-fee-resolve"),
]
