from django.urls import path

from orders import views as v
from orders.participant_views import OrderParticipantDetailView
from orders.task_views import OrderTaskDetailView

urlpatterns = [
    path("orders/", v.OrderListCreateView.as_view(), name="order-list"),
    path("orders/<uuid:order_id>/", v.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<uuid:order_id>/transition/", v.OrderTransitionView.as_view(), name="order-transition"),
    path("orders/<uuid:order_id>/cancel/", v.OrderCancelView.as_view(), name="order-cancel"),
    path("orders/<uuid:order_id>/reassign/", v.OrderReassignView.as_view(), name="order-reassign"),
    path(
        "orders/<uuid:order_id>/participants/", v.OrderParticipantsView.as_view(), name="order-participants"
    ),
    path(
        "orders/<uuid:order_id>/participants/<uuid:participant_id>/",
        OrderParticipantDetailView.as_view(),
        name="order-participant-detail",
    ),
    path("orders/<uuid:order_id>/route/", v.OrderRouteView.as_view(), name="order-route"),
    path("orders/<uuid:order_id>/overview/", v.OrderOverviewView.as_view(), name="order-overview"),
    path("orders/<uuid:order_id>/history/", v.OrderHistoryView.as_view(), name="order-history"),
    path("orders/<uuid:order_id>/tasks/", v.OrderTasksView.as_view(), name="order-tasks"),
    path(
        "orders/<uuid:order_id>/tasks/<uuid:task_id>/",
        OrderTaskDetailView.as_view(),
        name="order-task-detail",
    ),
    path(
        "orders/<uuid:order_id>/allowed-actions/",
        v.OrderAllowedActionsView.as_view(),
        name="order-allowed-actions",
    ),
    path("orders/<uuid:order_id>/duplicate/", v.OrderDuplicateView.as_view(), name="order-duplicate"),
    path(
        "orders/<uuid:order_id>/finance-summary/",
        v.OrderFinanceSummaryView.as_view(),
        name="order-finance-summary",
    ),
]
