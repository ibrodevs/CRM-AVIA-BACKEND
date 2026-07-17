from django.urls import path

from notifications import views as v

urlpatterns = [
    path("notifications/", v.NotificationListView.as_view(), name="notification-list"),
    path(
        "notifications/<int:notification_id>/read/",
        v.NotificationReadView.as_view(),
        name="notification-read",
    ),
    path(
        "notifications/<int:notification_id>/pin/", v.NotificationPinView.as_view(), name="notification-pin"
    ),
    path(
        "notifications/<int:notification_id>/dismiss/",
        v.NotificationDismissView.as_view(),
        name="notification-dismiss",
    ),
    path("notifications/read-all/", v.NotificationReadAllView.as_view(), name="notification-read-all"),
    path(
        "notifications/dismiss-read/",
        v.NotificationDismissReadView.as_view(),
        name="notification-dismiss-read",
    ),
    path("notification-rules/", v.NotificationRulesView.as_view(), name="notification-rules"),
]
