from django.urls import path

from common.drafts import DraftDetailView, DraftListView
from common.views import (
    EventFeedView,
    JobCancelView,
    JobDetailView,
    WorkspaceActionListCreateView,
    WorkspaceSettingView,
)

urlpatterns = [
    path("work-drafts/", DraftListView.as_view()),
    path("work-drafts/<uuid:pk>/", DraftDetailView.as_view()),
    path("events/", EventFeedView.as_view(), name="event-feed"),
    path("jobs/<uuid:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:job_id>/cancel/", JobCancelView.as_view(), name="job-cancel"),
    path("workspace-settings/<str:namespace>/", WorkspaceSettingView.as_view(), name="workspace-setting"),
    path("workspace-actions/", WorkspaceActionListCreateView.as_view(), name="workspace-actions"),
]
