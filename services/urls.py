from django.urls import path

from services import views as v

urlpatterns = [
    path("services/", v.ServiceListView.as_view(), name="service-list"),
    path("services/<uuid:service_id>/", v.ServiceDetailView.as_view(), name="service-detail"),
    path("services/search/", v.SearchCreateView.as_view(), name="service-search-create-v1"),
    path("services/search/<uuid:search_id>/", v.SearchDetailView.as_view(), name="service-search-detail-v1"),
    path(
        "services/search/<uuid:search_id>/offers/",
        v.SearchOffersView.as_view(),
        name="service-search-offers-v1",
    ),
    path(
        "services/search/<uuid:search_id>/cancel/",
        v.SearchCancelView.as_view(),
        name="service-search-cancel-v1",
    ),
    path("service-searches/", v.SearchCreateView.as_view(), name="service-search-create"),
    path("service-searches/<uuid:search_id>/", v.SearchDetailView.as_view(), name="service-search-detail"),
    path(
        "service-searches/<uuid:search_id>/cancel/",
        v.SearchCancelView.as_view(),
        name="service-search-cancel",
    ),
    path(
        "service-searches/<uuid:search_id>/offers/",
        v.SearchOffersView.as_view(),
        name="service-search-offers",
    ),
    path(
        "service-offers/<uuid:offer_id>/revalidate/", v.OfferRevalidateView.as_view(), name="offer-revalidate"
    ),
    path(
        "service-offers/<uuid:offer_id>/fare-rules/", v.OfferFareRulesView.as_view(), name="offer-fare-rules"
    ),
    path("service-offers/compare/", v.OfferCompareView.as_view(), name="offer-compare"),
    path("service-offers/manual/", v.ManualOfferCreateView.as_view(), name="offer-manual"),
    path("orders/<uuid:order_id>/services/", v.OrderServicesView.as_view(), name="order-services"),
    path(
        "services/<uuid:service_id>/transition/", v.ServiceTransitionView.as_view(), name="service-transition"
    ),
    path("services/<uuid:service_id>/revalidate/", v.ServiceRevalidateView.as_view(), name="service-revalidate"),
    path("services/<uuid:service_id>/book/", v.ServiceBookView.as_view(), name="service-book"),
    path("services/<uuid:service_id>/issue/", v.ServiceIssueView.as_view(), name="service-issue"),
    path("services/<uuid:service_id>/cancel/", v.ServiceCancelView.as_view(), name="service-cancel"),
    path("services/<uuid:service_id>/passengers/", v.ServicePassengersView.as_view(), name="service-passengers"),
    path("services/<uuid:service_id>/manual-book/", v.ServiceManualBookView.as_view(), name="service-manual-book"),
    path("services/<uuid:service_id>/manual-issue/", v.ServiceManualIssueView.as_view(), name="service-manual-issue"),
    path("services/<uuid:service_id>/extras/", v.ServiceExtrasView.as_view(), name="service-extras"),
    path(
        "services/<uuid:service_id>/responsible/",
        v.ServiceResponsibleView.as_view(),
        name="service-responsible",
    ),
]
