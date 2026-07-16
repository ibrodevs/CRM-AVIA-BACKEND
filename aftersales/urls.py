from django.urls import path

from aftersales import views as v

urlpatterns = [
    path("after-sales/", v.CaseListCreateView.as_view(), name="aftersale-list"),
    path("after-sales/<uuid:case_id>/", v.CaseDetailView.as_view(), name="aftersale-detail"),
    path("after-sales/<uuid:case_id>/quote/", v.CaseQuoteView.as_view(),
         name="aftersale-quote"),
    path("after-sales/<uuid:case_id>/transition/", v.CaseTransitionView.as_view(),
         name="aftersale-transition"),
    path("after-sales/<uuid:case_id>/send-for-approval/", v.CaseSendForApprovalView.as_view(),
         name="aftersale-send-approval"),
    path("after-sales/<uuid:case_id>/client-approve/", v.CaseClientApproveView.as_view(),
         name="aftersale-client-approve"),
    path("after-sales/<uuid:case_id>/submit-to-supplier/",
         v.CaseSubmitToSupplierView.as_view(), name="aftersale-submit"),
    path("after-sales/<uuid:case_id>/execute/", v.CaseExecuteView.as_view(),
         name="aftersale-execute"),
    path("after-sales/<uuid:case_id>/cancel/", v.CaseCancelView.as_view(),
         name="aftersale-cancel"),
    path("after-sales/<uuid:case_id>/documents/", v.CaseDocumentsView.as_view(),
         name="aftersale-documents"),
    path("after-sales/<uuid:case_id>/history/", v.CaseHistoryView.as_view(),
         name="aftersale-history"),
]
