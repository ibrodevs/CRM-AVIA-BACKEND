from django.urls import path

from documents import views as v

urlpatterns = [
    path("documents/", v.DocumentListCreateView.as_view(), name="document-list"),
    path("documents/<uuid:document_id>/versions/", v.DocumentVersionsView.as_view(),
         name="document-versions"),
    path("documents/<uuid:document_id>/generate/", v.DocumentGenerateView.as_view(),
         name="document-generate"),
    path("documents/<uuid:document_id>/sign/", v.DocumentSignView.as_view(),
         name="document-sign"),
    path("documents/<uuid:document_id>/void/", v.DocumentVoidView.as_view(),
         name="document-void"),
    path("documents/<uuid:document_id>/send/", v.DocumentSendView.as_view(),
         name="document-send"),
    path("documents/<uuid:document_id>/download/", v.DocumentDownloadView.as_view(),
         name="document-download"),
    path("document-templates/", v.DocumentTemplatesView.as_view(),
         name="document-templates"),
    path("receipt-imports/", v.ReceiptImportCreateView.as_view(), name="receipt-imports"),
    path("receipt-imports/<uuid:import_id>/result/", v.ReceiptImportResultView.as_view(),
         name="receipt-import-result"),
    path("receipt-imports/<uuid:import_id>/confirm/", v.ReceiptImportConfirmView.as_view(),
         name="receipt-import-confirm"),
]
