from django.urls import path

from groups_app import views as v

urlpatterns = [
    path("passenger-groups/", v.PassengerGroupListCreateView.as_view(),
         name="passenger-groups"),
    path("group-orders/", v.GroupOrderListCreateView.as_view(), name="group-orders"),
    path("group-orders/<uuid:group_order_id>/", v.GroupOrderDetailView.as_view(),
         name="group-order-detail"),
    path("group-orders/<uuid:group_order_id>/transition/",
         v.GroupOrderTransitionView.as_view(), name="group-order-transition"),
    path("group-orders/<uuid:group_order_id>/blocks/", v.GroupBlocksView.as_view(),
         name="group-order-blocks"),
    path("group-orders/<uuid:group_order_id>/matrix/", v.GroupMatrixView.as_view(),
         name="group-order-matrix"),
    path("group-orders/<uuid:group_order_id>/mass-actions/", v.GroupMassActionView.as_view(),
         name="group-order-mass-actions"),
    path("group-orders/<uuid:group_order_id>/requests/", v.GroupRequestsView.as_view(),
         name="group-order-requests"),
    path("group-orders/<uuid:group_order_id>/supplier-responses/",
         v.GroupSupplierResponsesView.as_view(), name="group-order-supplier-responses"),
    path("roster-imports/", v.RosterImportCreateView.as_view(), name="roster-imports"),
    path("roster-imports/<uuid:import_id>/preview/", v.RosterImportPreviewView.as_view(),
         name="roster-import-preview"),
    path("roster-imports/<uuid:import_id>/apply/", v.RosterImportApplyView.as_view(),
         name="roster-import-apply"),
    path("roster-imports/<uuid:import_id>/export/", v.RosterImportExportView.as_view(),
         name="roster-import-export"),
]
