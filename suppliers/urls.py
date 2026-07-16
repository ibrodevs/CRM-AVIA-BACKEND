from django.urls import path

from suppliers import views as v

urlpatterns = [
    path("suppliers/", v.SupplierListCreateView.as_view(), name="supplier-list"),
    path("suppliers/<uuid:supplier_id>/", v.SupplierDetailView.as_view(), name="supplier-detail"),
    path("suppliers/<uuid:supplier_id>/credentials/", v.SupplierCredentialsView.as_view(),
         name="supplier-credentials"),
    path("suppliers/<uuid:supplier_id>/check-connection/", v.SupplierCheckConnectionView.as_view(),
         name="supplier-check-connection"),
    path("suppliers/<uuid:supplier_id>/markup-rules/", v.SupplierMarkupRulesView.as_view(),
         name="supplier-markup-rules"),
    path("supplier-search-priorities/", v.SearchPriorityListCreateView.as_view(),
         name="supplier-search-priorities"),
]
