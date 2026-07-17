from django.urls import include, path

urlpatterns = [
    path("auth/", include("accounts.auth_urls")),
    path("", include("accounts.urls")),
    path("", include("common.urls")),
    path("", include("crm.urls")),
    path("", include("travel_policy.urls")),
    path("", include("suppliers.urls")),
    path("", include("orders.urls")),
    path("", include("calendar_app.urls")),
    path("", include("services.urls")),
    path("", include("offers.urls")),
    path("", include("booking.urls")),
    path("", include("groups_app.urls")),
    path("", include("documents.urls")),
    path("", include("communications.urls")),
    path("", include("finance.urls")),
    path("", include("aftersales.urls")),
    path("", include("search.urls")),
    path("", include("notifications.urls")),
    path("", include("workforce.urls")),
    path("", include("reports.urls")),
    path("", include("integrations.urls")),
]

from common.meta_views import MetaView  # noqa: E402

urlpatterns.append(path("meta/", MetaView.as_view(), name="meta"))
