from django.urls import path

from common.views import EventFeedView, JobCancelView, JobDetailView

urlpatterns = [
    path("events/", EventFeedView.as_view(), name="event-feed"),
    path("jobs/<uuid:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:job_id>/cancel/", JobCancelView.as_view(), name="job-cancel"),
]
