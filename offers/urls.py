from django.urls import path

from offers import views as v

urlpatterns = [
    path("proposals/", v.ProposalListCreateView.as_view(), name="proposal-list"),
    path("proposals/<uuid:proposal_id>/", v.ProposalDetailView.as_view(), name="proposal-detail"),
    path("proposals/<uuid:proposal_id>/draft/", v.ProposalDraftReplaceView.as_view(), name="proposal-draft"),
    path(
        "proposals/<uuid:proposal_id>/versions/", v.ProposalVersionsView.as_view(), name="proposal-versions"
    ),
    path("proposals/<uuid:proposal_id>/prepare/", v.ProposalPrepareView.as_view(), name="proposal-prepare"),
    path("proposals/<uuid:proposal_id>/send/", v.ProposalSendView.as_view(), name="proposal-send"),
    path("proposals/<uuid:proposal_id>/approve/", v.ProposalApproveView.as_view(), name="proposal-approve"),
    path("proposals/<uuid:proposal_id>/reject/", v.ProposalRejectView.as_view(), name="proposal-reject"),
    path("proposals/<uuid:proposal_id>/archive/", v.ProposalArchiveView.as_view(), name="proposal-archive"),
    path("proposals/<uuid:proposal_id>/pdf/", v.ProposalPdfView.as_view(), name="proposal-pdf"),
    path("proposal-templates/", v.ProposalTemplatesView.as_view(), name="proposal-templates"),
    path("proposal-templates/<uuid:template_id>/", v.ProposalTemplateDetailView.as_view(), name="proposal-template-detail"),
    path("service-cards/", v.ServiceCardCreateView.as_view(), name="service-card-create"),
    path("service-cards/<uuid:card_id>/send/", v.ServiceCardSendView.as_view(), name="service-card-send"),
    path(
        "service-cards/<uuid:card_id>/expire/", v.ServiceCardExpireView.as_view(), name="service-card-expire"
    ),
    path("public/service-cards/<str:token>/", v.PublicServiceCardView.as_view(), name="public-service-card"),
    path(
        "public/service-cards/<str:token>/respond/",
        v.PublicServiceCardRespondView.as_view(),
        name="public-service-card-respond",
    ),
]
