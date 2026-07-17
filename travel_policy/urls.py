from django.urls import path

from travel_policy import views as v

urlpatterns = [
    path(
        "companies/<uuid:company_id>/travel-policies/",
        v.CompanyTravelPoliciesView.as_view(),
        name="company-travel-policies",
    ),
    path(
        "travel-policies/<uuid:policy_id>/", v.TravelPolicyDetailView.as_view(), name="travel-policy-detail"
    ),
    path(
        "travel-policies/<uuid:policy_id>/check/",
        v.TravelPolicyCheckView.as_view(),
        name="travel-policy-check",
    ),
]
