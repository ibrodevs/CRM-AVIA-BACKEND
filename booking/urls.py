from django.urls import path

from booking import views as v

urlpatterns = [
    path(
        "booking-workflows/<uuid:workflow_id>/cancel-transfer/",
        v.WorkflowTransferCancellationView.as_view(),
        name="workflow-cancel-transfer",
    ),
    path("booking-workflows/<uuid:workflow_id>/void/", v.WorkflowVoidView.as_view(), name="workflow-void"),
    path(
        "booking-workflows/<uuid:workflow_id>/resume/", v.WorkflowResumeView.as_view(), name="workflow-resume"
    ),
    path("booking-workflows/", v.WorkflowCreateView.as_view(), name="workflow-create"),
    path(
        "booking-workflows/<uuid:workflow_id>/preflight/",
        v.WorkflowPreflightView.as_view(),
        name="workflow-preflight",
    ),
    path("booking-workflows/<uuid:workflow_id>/start/", v.WorkflowStartView.as_view(), name="workflow-start"),
    path(
        "booking-workflows/<uuid:workflow_id>/status/", v.WorkflowStatusView.as_view(), name="workflow-status"
    ),
    path("booking-workflows/<uuid:workflow_id>/issue/", v.WorkflowIssueView.as_view(), name="workflow-issue"),
    path(
        "booking-workflows/<uuid:workflow_id>/status-inquiry/",
        v.WorkflowInquiryView.as_view(),
        name="workflow-inquiry",
    ),
    path(
        "booking-workflows/<uuid:workflow_id>/cancel/", v.WorkflowCancelView.as_view(), name="workflow-cancel"
    ),
]
