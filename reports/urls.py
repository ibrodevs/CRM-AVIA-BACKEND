from django.urls import path

from reports.views import DashboardView, ReportSummaryView, SavedReportsView

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/summary/", ReportSummaryView.as_view(), name="report-summary"),
    path("reports/saved/", SavedReportsView.as_view(), name="report-saved"),
]
