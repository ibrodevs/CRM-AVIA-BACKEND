from django.urls import path

from integrations import views as v

urlpatterns = [
    path("integration-operations/", v.IntegrationLogListView.as_view(),
         name="integration-operations"),
    path("integration-incidents/", v.IncidentListView.as_view(),
         name="integration-incidents"),
    path("integration-incidents/<int:incident_id>/assign/", v.IncidentAssignView.as_view(),
         name="incident-assign"),
    path("integration-incidents/<int:incident_id>/retry/", v.IncidentRetryView.as_view(),
         name="incident-retry"),
    path("integration-incidents/<int:incident_id>/snooze/", v.IncidentSnoozeView.as_view(),
         name="incident-snooze"),
    path("integration-incidents/<int:incident_id>/switch-supplier/",
         v.IncidentSwitchSupplierView.as_view(), name="incident-switch-supplier"),
    path("integration-incidents/<int:incident_id>/resolve/", v.IncidentResolveView.as_view(),
         name="incident-resolve"),
    path("integration-incidents/<int:incident_id>/reopen/", v.IncidentReopenView.as_view(),
         name="incident-reopen"),
    path("integration-incidents/<int:incident_id>/escalate/", v.IncidentEscalateView.as_view(),
         name="incident-escalate"),
    path("integration-error-codes/", v.ErrorCodeListView.as_view(),
         name="integration-error-codes"),
]
