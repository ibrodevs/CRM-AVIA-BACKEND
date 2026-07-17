from django.urls import path

from finance import views as v

urlpatterns = [
    path("finance/overview/", v.FinanceOverviewView.as_view(), name="finance-overview"),
    path("finance/accounts/", v.AccountListView.as_view(), name="finance-accounts"),
    path("finance/transactions/", v.TransactionListView.as_view(), name="finance-transactions"),
    path("finance/obligations/", v.ObligationListCreateView.as_view(), name="finance-obligations"),
    path("finance/payments/", v.PaymentListCreateView.as_view(), name="finance-payments"),
    path(
        "finance/payments/<uuid:payment_id>/confirm/", v.PaymentConfirmView.as_view(), name="payment-confirm"
    ),
    path(
        "finance/payments/<uuid:payment_id>/allocate/",
        v.PaymentAllocateView.as_view(),
        name="payment-allocate",
    ),
    path("finance/refunds/", v.RefundListCreateView.as_view(), name="finance-refunds"),
    path("finance/refunds/<uuid:refund_id>/execute/", v.RefundExecuteView.as_view(), name="refund-execute"),
    path("finance/cashflow/", v.CashflowView.as_view(), name="finance-cashflow"),
    path("finance/economics/", v.EconomicsView.as_view(), name="finance-economics"),
    path("finance/analytics/", v.EconomicsView.as_view(), name="finance-analytics"),
    path(
        "finance/reconciliation/imports/", v.ReconciliationImportView.as_view(), name="reconciliation-imports"
    ),
    path(
        "finance/reconciliation/imports/<uuid:import_id>/match/",
        v.ReconciliationMatchView.as_view(),
        name="reconciliation-match",
    ),
    path(
        "companies/<uuid:company_id>/finance-summary/",
        v.CompanyFinanceSummaryView.as_view(),
        name="company-finance-summary",
    ),
]
