from django.urls import path

from crm import views as v

urlpatterns = [
    path("persons/", v.PersonListCreateView.as_view(), name="person-list"),
    path("persons/<uuid:person_id>/", v.PersonDetailView.as_view(), name="person-detail"),
    path("persons/<uuid:person_id>/documents/", v.PersonDocumentsView.as_view(), name="person-documents"),
    path(
        "persons/<uuid:person_id>/loyalty-cards/",
        v.PersonLoyaltyCardsView.as_view(),
        name="person-loyalty-cards",
    ),
    path("clients/", v.ClientListCreateView.as_view(), name="client-list"),
    path("companies/", v.CompanyListCreateView.as_view(), name="company-list"),
    path("companies/<uuid:company_id>/", v.CompanyDetailView.as_view(), name="company-detail"),
    path(
        "companies/<uuid:company_id>/employees/", v.CompanyEmployeesView.as_view(), name="company-employees"
    ),
    path(
        "companies/<uuid:company_id>/departments/",
        v.CompanyDepartmentsView.as_view(),
        name="company-departments",
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
]
