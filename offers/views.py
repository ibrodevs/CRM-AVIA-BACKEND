from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError, TransitionForbiddenError
from common.idempotency import idempotent_command
from common.locking import check_version
from common.outbox import emit_event
from common.pagination import DefaultPagination
from offers.models import (
    PROPOSAL_TRANSITIONS,
    Proposal,
    ProposalItem,
    ProposalNumberCounter,
    ProposalTemplate,
    ProposalVariant,
    ProposalVersion,
    ServiceCard,
    ServiceCardDelivery,
    ServiceCardResponse,
)
from orders.selectors import get_order_or_404


class ProposalItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProposalItem
        fields = [
            "id",
            "offer",
            "service",
            "title",
            "description",
            "quantity",
            "price_amount",
            "price_currency",
        ]
        read_only_fields = ["id"]


class ProposalVariantSerializer(serializers.ModelSerializer):
    items = ProposalItemSerializer(many=True, read_only=True)

    class Meta:
        model = ProposalVariant
        fields = ["id", "name", "sequence", "status", "comment", "items"]
        read_only_fields = ["id", "sequence", "status"]


class ProposalSerializer(serializers.ModelSerializer):
    variants = ProposalVariantSerializer(many=True, read_only=True)

    class Meta:
        model = Proposal
        fields = [
            "id",
            "number",
            "order",
            "type",
            "purpose",
            "status",
            "currency",
            "valid_until",
            "template",
            "current_version",
            "approved_variant",
            "variants",
            "created_at",
            "version",
        ]
        read_only_fields = [
            "id",
            "number",
            "status",
            "current_version",
            "approved_variant",
            "created_at",
            "version",
        ]


def _get_proposal(request, proposal_id) -> Proposal:
    proposal = Proposal.objects.filter(pk=proposal_id, tenant_id=request.user.tenant_id).first()
    if proposal is None:
        raise ApiError(code="NOT_FOUND", message="КП не найдено", status_code=404)
    return proposal


def _proposal_snapshot(proposal: Proposal) -> dict:
    return {
        "number": proposal.number,
        "status": proposal.status,
        "currency": proposal.currency,
        "valid_until": proposal.valid_until.isoformat() if proposal.valid_until else None,
        "variants": [
            {
                "name": v.name,
                "sequence": v.sequence,
                "status": v.status,
                "items": [
                    {
                        "title": i.title,
                        "description": i.description,
                        "quantity": i.quantity,
                        "price_amount": str(i.price_amount),
                        "price_currency": i.price_currency,
                    }
                    for i in v.items.all()
                ],
            }
            for v in proposal.variants.all().order_by("sequence")
        ],
    }


def _bump_proposal_version(proposal: Proposal, user) -> ProposalVersion:
    proposal.current_version += 1
    proposal.save(update_fields=["current_version"])
    return ProposalVersion.objects.create(
        proposal=proposal,
        version=proposal.current_version,
        snapshot=_proposal_snapshot(proposal),
        created_by=user,
    )


class ProposalListCreateView(GenericAPIView):
    permission_classes = [require("offers.view")]
    pagination_class = DefaultPagination
    serializer_class = ProposalSerializer

    def get(self, request):
        qs = (
            Proposal.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True)
            .prefetch_related("variants__items")
            .order_by("-created_at")
        )
        if order_id := request.query_params.get("order"):
            qs = qs.filter(order_id=order_id)
        if proposal_status := request.query_params.get("status"):
            qs = qs.filter(status=proposal_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(ProposalSerializer(page, many=True).data)

    def post(self, request):
        self.permission_classes = [require("offers.create")]
        self.check_permissions(request)
        serializer = ProposalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_order_or_404(request.user, serializer.validated_data["order"].pk)
        with transaction.atomic():
            proposal = serializer.save(
                tenant_id=request.user.tenant_id,
                number=ProposalNumberCounter.next_number(request.user.tenant_id),
                created_by=request.user,
            )
            for index, variant in enumerate(request.data.get("variants", []), start=1):
                variant_obj = ProposalVariant.objects.create(
                    tenant_id=request.user.tenant_id,
                    proposal=proposal,
                    name=variant.get("name", f"Вариант {index}"),
                    sequence=index,
                    created_by=request.user,
                )
                for item in variant.get("items", []):
                    item_serializer = ProposalItemSerializer(data=item)
                    item_serializer.is_valid(raise_exception=True)
                    ProposalItem.objects.create(
                        tenant_id=request.user.tenant_id,
                        variant=variant_obj,
                        created_by=request.user,
                        **item_serializer.validated_data,
                    )
        audit(
            "offers.proposal_created",
            actor=request.user,
            resource=proposal,
            request=request,
            after={"order": str(order.id)},
        )
        proposal.refresh_from_db()
        return Response(ProposalSerializer(proposal).data, status=http.HTTP_201_CREATED)


class ProposalDetailView(APIView):
    permission_classes = [require("offers.view")]

    def get(self, request, proposal_id):
        return Response(ProposalSerializer(_get_proposal(request, proposal_id)).data)


class ProposalVersionsView(APIView):
    permission_classes = [require("offers.view")]

    def get(self, request, proposal_id):
        proposal = _get_proposal(request, proposal_id)
        return Response(
            [
                {
                    "version": v.version,
                    "snapshot": v.snapshot,
                    "created_at": v.created_at,
                    "created_by": str(v.created_by_id) if v.created_by_id else None,
                }
                for v in proposal.versions.order_by("-version")
            ]
        )


def _proposal_transition(request, proposal: Proposal, target: str, *, reason: str = "") -> None:
    allowed = PROPOSAL_TRANSITIONS.get(proposal.status, set())
    if target not in allowed:
        raise TransitionForbiddenError(
            code="PROPOSAL_STATUS_TRANSITION_FORBIDDEN",
            message=f"Переход из {proposal.status} в {target} запрещён",
            details={"current_status": proposal.status, "allowed": sorted(allowed)},
        )
    old = proposal.status
    proposal.status = target
    proposal.version += 1
    proposal.updated_by = request.user
    proposal.save(update_fields=["status", "version", "updated_by", "updated_at"])
    emit_event(
        "order.updated",
        proposal.order,
        payload={"action": "proposal_status", "proposal": str(proposal.id), "to": target},
    )
    audit(
        f"offers.proposal_{target}",
        actor=request.user,
        resource=proposal,
        request=request,
        reason=reason,
        before={"status": old},
        after={"status": target},
    )


class ProposalPrepareView(APIView):
    permission_classes = [require("offers.change")]

    def post(self, request, proposal_id):
        with transaction.atomic():
            proposal = Proposal.objects.select_for_update().get(pk=_get_proposal(request, proposal_id).pk)
            check_version(proposal, request.data.get("version"))
            if not proposal.variants.exists():
                raise ApiError(code="PROPOSAL_EMPTY", message="КП не содержит вариантов", status_code=409)
            _proposal_transition(request, proposal, Proposal.Status.PREPARED)
            _bump_proposal_version(proposal, request.user)
        return Response(ProposalSerializer(proposal).data)


class ProposalSendView(APIView):
    permission_classes = [require("offers.send")]

    @idempotent_command("offers.proposal_send")
    def post(self, request, proposal_id):
        with transaction.atomic():
            proposal = Proposal.objects.select_for_update().get(pk=_get_proposal(request, proposal_id).pk)
            check_version(proposal, request.data.get("version"))
            _proposal_transition(request, proposal, Proposal.Status.SENT)
            if not proposal.versions.filter(version=proposal.current_version).exists():
                _bump_proposal_version(proposal, request.user)
        return Response(ProposalSerializer(proposal).data)


class ProposalApproveView(APIView):
    permission_classes = [require("offers.approve")]

    @idempotent_command("offers.proposal_approve")
    def post(self, request, proposal_id):
        variant_id = request.data.get("variant")
        with transaction.atomic():
            proposal = Proposal.objects.select_for_update().get(pk=_get_proposal(request, proposal_id).pk)
            check_version(proposal, request.data.get("version"))
            variant = proposal.variants.filter(pk=variant_id).first()
            if variant is None:
                raise ApiError(
                    code="VALIDATION_ERROR",
                    message="Вариант не найден",
                    fields={"variant": ["Обязателен id варианта"]},
                    status_code=400,
                )

            if proposal.valid_until and proposal.valid_until < timezone.now():
                raise ApiError(code="PROPOSAL_EXPIRED", message="Срок действия КП истёк", status_code=409)
            for item in variant.items.all():
                if item.offer and item.offer.expires_at and item.offer.expires_at < timezone.now():
                    raise ApiError(
                        code="OFFER_EXPIRED",
                        message=f"Позиция «{item.title}» устарела; обновите цены",
                        status_code=409,
                    )
            _proposal_transition(request, proposal, Proposal.Status.APPROVED)
            variant.status = "approved"
            variant.save(update_fields=["status"])
            proposal.variants.exclude(pk=variant.pk).update(status="rejected")
            proposal.approved_variant = variant
            proposal.save(update_fields=["approved_variant"])

            if request.data.get("create_services"):
                from services.models import OrderService

                for item in variant.items.select_related("offer"):
                    if item.service_id or item.offer_id is None:
                        continue
                    OrderService.objects.create(
                        tenant_id=proposal.tenant_id,
                        order=proposal.order,
                        kind=item.offer.kind,
                        title=item.title,
                        supplier=item.offer.supplier,
                        currency=item.price_currency,
                        client_total=item.price_amount,
                        provider_snapshot=item.offer.raw_snapshot,
                        created_by=request.user,
                    )
        return Response(ProposalSerializer(proposal).data)


class ProposalRejectView(APIView):
    permission_classes = [require("offers.approve", "offers.change")]

    def post(self, request, proposal_id):
        with transaction.atomic():
            proposal = Proposal.objects.select_for_update().get(pk=_get_proposal(request, proposal_id).pk)
            check_version(proposal, request.data.get("version"))
            _proposal_transition(
                request, proposal, Proposal.Status.REJECTED, reason=str(request.data.get("reason", ""))
            )
        return Response(ProposalSerializer(proposal).data)


class ProposalArchiveView(APIView):
    permission_classes = [require("offers.archive")]

    def post(self, request, proposal_id):
        with transaction.atomic():
            proposal = Proposal.objects.select_for_update().get(pk=_get_proposal(request, proposal_id).pk)
            check_version(proposal, request.data.get("version"))
            _proposal_transition(request, proposal, Proposal.Status.ARCHIVED)
            proposal.archived_at = timezone.now()
            proposal.save(update_fields=["archived_at"])
        return Response(ProposalSerializer(proposal).data)


class ProposalPdfView(APIView):
    """PDF генерируется сервером из сохранённой версии (ТЗ §12.1)."""

    permission_classes = [require("offers.view")]

    def get(self, request, proposal_id):
        from django.http import HttpResponse

        from offers.pdf import render_proposal_pdf

        proposal = _get_proposal(request, proposal_id)
        version_number = request.query_params.get("proposal_version")
        version = (
            proposal.versions.filter(version=version_number).first()
            if version_number
            else proposal.versions.order_by("-version").first()
        )
        if version is None:
            raise ApiError(code="NO_VERSION", message="Сначала подготовьте КП (prepare)", status_code=409)
        pdf_bytes = render_proposal_pdf(version.snapshot)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{proposal.number}-v{version.version}.pdf"'
        return response


class ProposalTemplatesView(APIView):
    permission_classes = [require("offers.view")]

    def get(self, request):
        templates = ProposalTemplate.objects.filter(
            tenant_id=request.user.tenant_id, archived_at__isnull=True
        )
        return Response(
            [
                {
                    "id": str(t.id),
                    "code": t.code,
                    "name": t.name,
                    "template_version": t.template_version,
                    "status": t.status,
                }
                for t in templates
            ]
        )

    def post(self, request):
        self.permission_classes = [require("offers.manage_templates")]
        self.check_permissions(request)
        code = str(request.data.get("code", "")).strip()
        name = str(request.data.get("name", "")).strip()
        if not code or not name:
            raise ApiError(code="VALIDATION_ERROR", message="Нужны code и name", status_code=400)
        last = (
            ProposalTemplate.objects.filter(tenant_id=request.user.tenant_id, code=code)
            .order_by("-template_version")
            .first()
        )
        template = ProposalTemplate.objects.create(
            tenant_id=request.user.tenant_id,
            code=code,
            name=name,
            body=str(request.data.get("body", "")),
            template_version=(last.template_version + 1) if last else 1,
            created_by=request.user,
        )
        return Response(
            {"id": str(template.id), "template_version": template.template_version},
            status=http.HTTP_201_CREATED,
        )


class ServiceCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCard
        fields = [
            "id",
            "order",
            "service",
            "offer",
            "kind",
            "scenario",
            "status",
            "valid_until",
            "price_snapshot",
            "content",
            "card_version",
            "created_at",
        ]
        read_only_fields = ["id", "status", "card_version", "created_at"]


class ServiceCardCreateView(APIView):
    permission_classes = [require("offers.create")]

    def post(self, request):
        serializer = ServiceCardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        card = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        audit("offers.card_created", actor=request.user, resource=card, request=request)
        return Response(
            {**ServiceCardSerializer(card).data, "public_token": card.public_token},
            status=http.HTTP_201_CREATED,
        )


def _get_card(request, card_id) -> ServiceCard:
    card = ServiceCard.objects.filter(pk=card_id, tenant_id=request.user.tenant_id).first()
    if card is None:
        raise ApiError(code="NOT_FOUND", message="Карточка не найдена", status_code=404)
    return card


class ServiceCardSendView(APIView):
    permission_classes = [require("offers.send")]

    @idempotent_command("offers.card_send")
    def post(self, request, card_id):
        channels = request.data.get("channels", ["internal"])
        with transaction.atomic():
            card = ServiceCard.objects.select_for_update().get(pk=_get_card(request, card_id).pk)
            if card.status not in (ServiceCard.Status.CREATED, ServiceCard.Status.SENT):
                raise ApiError(
                    code="CARD_NOT_SENDABLE", message=f"Карточка в статусе {card.status}", status_code=409
                )
            for channel in channels:
                ServiceCardDelivery.objects.create(
                    tenant_id=card.tenant_id,
                    card=card,
                    channel=channel,
                    recipient=str(request.data.get("recipient", "")),
                    created_by=request.user,
                )
            card.status = ServiceCard.Status.SENT
            card.save(update_fields=["status"])
            emit_event("service.updated", card, payload={"action": "card_sent", "channels": channels})
            audit(
                "offers.card_sent",
                actor=request.user,
                resource=card,
                request=request,
                after={"channels": channels},
            )
        return Response(ServiceCardSerializer(card).data)


class ServiceCardExpireView(APIView):
    permission_classes = [require("offers.change")]

    def post(self, request, card_id):
        card = _get_card(request, card_id)
        card.status = ServiceCard.Status.EXPIRED
        card.save(update_fields=["status"])
        audit("offers.card_expired", actor=request.user, resource=card, request=request)
        return Response(ServiceCardSerializer(card).data)


class PublicResponseThrottle(AnonRateThrottle):
    scope = "public_response"


class PublicServiceCardView(APIView):
    """Публичное представление карточки без CRM JWT (ТЗ §12.1, §12.2)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [PublicResponseThrottle]

    def get(self, request, token):
        card = ServiceCard.objects.filter(public_token=token).first()
        if card is None:
            raise ApiError(code="NOT_FOUND", message="Карточка не найдена", status_code=404)
        if card.status == ServiceCard.Status.SENT:
            card.status = ServiceCard.Status.VIEWED
            card.save(update_fields=["status"])
            emit_event("service.updated", card, payload={"action": "card_viewed"}, tenant_id=card.tenant_id)
        return Response(
            {
                "kind": card.kind,
                "status": card.status,
                "valid_until": card.valid_until,
                "content": card.content,
                "price": card.price_snapshot,
                "card_version": card.card_version,
            }
        )


class PublicServiceCardRespondView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [PublicResponseThrottle]

    ACTIONS = {"choose", "decline", "request_alternative", "contact_operator"}

    def post(self, request, token):
        action = str(request.data.get("action", ""))
        if action not in self.ACTIONS:
            raise ApiError(
                code="VALIDATION_ERROR", message=f"action из {sorted(self.ACTIONS)}", status_code=400
            )
        with transaction.atomic():
            card = ServiceCard.objects.select_for_update().filter(public_token=token).first()
            if card is None:
                raise ApiError(code="NOT_FOUND", message="Карточка не найдена", status_code=404)
            if card.valid_until and card.valid_until < timezone.now():
                card.status = ServiceCard.Status.EXPIRED
                card.save(update_fields=["status"])
                raise ApiError(code="CARD_EXPIRED", message="Срок действия истёк", status_code=409)
            existing = card.responses.filter(
                card_version=card.card_version, action__in=["choose", "decline"]
            ).first()
            if existing is not None:
                return Response(
                    {"action": existing.action, "status": card.status, "responded_at": existing.created_at}
                )
            response_obj = ServiceCardResponse.objects.create(
                card=card,
                card_version=card.card_version,
                action=action,
                comment=str(request.data.get("comment", "")),
                channel="public_link",
            )
            if action == "choose":
                card.status = ServiceCard.Status.CHOSEN
            elif action == "decline":
                card.status = ServiceCard.Status.DECLINED
            card.save(update_fields=["status"])
            emit_event(
                "service.updated", card, payload={"action": f"card_{action}"}, tenant_id=card.tenant_id
            )
        return Response({"action": action, "status": card.status, "responded_at": response_obj.created_at})
