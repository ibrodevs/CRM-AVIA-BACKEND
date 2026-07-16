from django.urls import path

from workforce import views as v

urlpatterns = [
    path("sla/queue/", v.SlaQueueView.as_view(), name="sla-queue"),
    path("shifts/current/", v.ShiftCurrentView.as_view(), name="shift-current"),
    path("shifts/start/", v.ShiftStartView.as_view(), name="shift-start"),
    path("shifts/<uuid:shift_id>/preview-close/", v.ShiftPreviewCloseView.as_view(),
         name="shift-preview-close"),
    path("shifts/<uuid:shift_id>/close/", v.ShiftCloseView.as_view(), name="shift-close"),
    path("shifts/<uuid:shift_id>/report/", v.ShiftReportView.as_view(), name="shift-report"),
    path("motivation/rules/", v.MotivationRulesView.as_view(), name="motivation-rules"),
    path("motivation/accruals/", v.MotivationAccrualsView.as_view(),
         name="motivation-accruals"),
]
