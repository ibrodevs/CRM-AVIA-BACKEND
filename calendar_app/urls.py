from django.urls import path

from calendar_app import views as v

urlpatterns = [
    path("trips/", v.TripListView.as_view(), name="trip-list"),
    path("trips/<uuid:trip_id>/conflicts/", v.TripConflictsView.as_view(), name="trip-conflicts"),
    path("calendar/events/", v.CalendarEventListCreateView.as_view(), name="calendar-events"),
    path(
        "calendar/events/check-duplicate/",
        v.CalendarEventCheckDuplicateView.as_view(),
        name="calendar-check-duplicate",
    ),
    path(
        "calendar/events/<uuid:event_id>/complete/",
        v.CalendarEventCompleteView.as_view(),
        name="calendar-event-complete",
    ),
    path(
        "calendar/events/<uuid:event_id>/reschedule/",
        v.CalendarEventRescheduleView.as_view(),
        name="calendar-event-reschedule",
    ),
    path("calendar/feed/", v.CalendarFeedView.as_view(), name="calendar-feed"),
]
