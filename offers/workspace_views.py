from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.locking import check_version
from offers.models import Proposal, ProposalDraftRevision, ProposalPublication, ProposalWorkspace
from offers.views import PublicResponseThrottle, _get_proposal
from offers.workspace import (
    all_options,
    effective,
    ensure_active,
    ensure_current,
    fail,
    normalize,
    record,
    refresh_option,
    serialize,
    text,
    validate_selection,
    workspace_for,
)


class WorkspaceView(APIView):
    permission_classes = [require("offers.view")]

    def get(self, request, proposal_id):
        return Response(serialize(workspace_for(_get_proposal(request, proposal_id))))

    def put(self, request, proposal_id):
        if not has_permission(request.user, "offers.change"):
            fail("Нет права редактировать КП", "PERMISSION_DENIED", 403)
        proposal = _get_proposal(request, proposal_id)
        with transaction.atomic():
            workspace = ProposalWorkspace.objects.select_for_update().get(pk=workspace_for(proposal).pk)
            check_version(workspace, request.data.get("version"))
            if request.data.get("order") and str(proposal.order_id) != str(request.data["order"]):
                from orders.selectors import get_order_or_404

                if proposal.order_id and workspace.revision:
                    fail("После публикации нельзя менять связанный заказ")
                proposal.order = get_order_or_404(request.user, request.data["order"])
                proposal.version += 1
                proposal.updated_by = request.user
                proposal.save(update_fields=["order", "version", "updated_by", "updated_at"])
            previous = workspace.draft
            publication = workspace.publications.filter(revision=workspace.revision).first()
            if publication:
                # API-check evidence is server-owned, never accepted from the browser.
                previous = effective(workspace, publication)
            ProposalDraftRevision.objects.get_or_create(
                workspace=workspace,
                version=workspace.version,
                defaults={"payload": workspace.draft, "created_by": request.user},
            )
            workspace.draft = normalize(
                request.data.get("draft"), proposal, previous=previous, allow_incomplete=True
            )
            workspace.version += 1
            workspace.save()
            ProposalDraftRevision.objects.create(
                workspace=workspace,
                version=workspace.version,
                payload=workspace.draft,
                created_by=request.user,
            )
            record(workspace, "draft_saved", actor=request.user)
        return Response(serialize(workspace))


class WorkspacePublishView(APIView):
    permission_classes = [require("offers.send")]

    def post(self, request, proposal_id):
        proposal = _get_proposal(request, proposal_id)
        with transaction.atomic():
            workspace = ProposalWorkspace.objects.select_for_update().get(pk=workspace_for(proposal).pk)
            check_version(workspace, request.data.get("version"))
            if proposal.status == Proposal.Status.ARCHIVED:
                fail("Архивное КП нельзя опубликовать", "PROPOSAL_CLOSED", 409)
            payload = normalize(workspace.draft, proposal, previous=workspace.draft)
            if (
                not payload["title"]
                or not all_options(payload)
                or any(not o["title"] for o in all_options(payload).values())
            ):
                fail("Заполните название КП и хотя бы одну услугу")
            if payload["mode"] in {"packages", "both"} and not payload["packages"]:
                fail("Добавьте готовый комплект или выберите свободный выбор услуг")
            if payload.get("valid_until") and parse_datetime(payload["valid_until"]) <= timezone.now():
                fail("Срок действия КП уже истёк")
            final = request.data.get("final") is True
            if final:
                selected = workspace.selection
                if not selected.get("option_ids"):
                    fail("Сначала получите выбор клиента")
                selected_options = all_options(payload)
                for oid in selected["option_ids"]:
                    row = selected_options.get(oid, {})
                    checked = workspace.checks.get(oid, {})
                    manual_override = row.get("check_source") == "manual" and (
                        row.get("checked_at") or ""
                    ) > checked.get("attempted_at", "")
                    if (
                        checked
                        and not manual_override
                        and (
                            checked.get("check_error")
                            or checked.get("price", row.get("price")) != row.get("price")
                            or checked.get("availability", row.get("availability")) != row.get("availability")
                        )
                    ):
                        fail(
                            "Условия изменились. Обновите черновик и проверьте выбранные услуги",
                            "CHECK_REQUIRED",
                            409,
                        )
                validate_selection(payload, selected["option_ids"], selected.get("package_id"), checked=True)
            else:
                # Verify packages without required-group constraints.
                for package in payload["packages"]:
                    validate_selection(payload, package["option_ids"], package["id"])
            workspace.draft = payload
            workspace.revision += 1
            ProposalPublication.objects.create(
                workspace=workspace,
                revision=workspace.revision,
                payload=payload,
                final=final,
                created_by=request.user,
            )
            workspace.checks = {}
            workspace.stage = "awaiting_approval" if final else "published"
            workspace.version += 1
            workspace.save()
            record(workspace, "final_published" if final else "published", actor=request.user)
        return Response(serialize(workspace))


class PublicWorkspaceBase(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicResponseThrottle]

    def workspace(self, token, *, lock=False):
        qs = ProposalWorkspace.objects.select_related("proposal", "proposal__order")
        if lock:
            qs = qs.select_for_update(of=("self",))
        workspace = qs.filter(public_token=token, revision__gt=0).first()
        if workspace is None or workspace.proposal.status == "archived" or workspace.proposal.archived_at:
            fail("Предложение не найдено или закрыто", "NOT_FOUND", 404)
        return workspace


class PublicWorkspaceView(PublicWorkspaceBase):
    def get(self, request, token):
        return Response(serialize(self.workspace(token), public=True))


class PublicWorkspaceRespondView(PublicWorkspaceBase):
    def post(self, request, token):
        action = request.data.get("action")
        if action not in {"selection", "approval", "comment", "alternative"}:
            fail("Неизвестное действие")
        with transaction.atomic():
            workspace = self.workspace(token, lock=True)
            ensure_current(workspace, request.data)
            publication = ensure_active(workspace)
            payload = effective(workspace, publication)
            data = {"comment": text(request.data.get("comment", ""))}
            if action in {"selection", "approval"}:
                ids = request.data.get("option_ids", [])
                if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
                    fail("Некорректный выбор услуг")
                package_id = request.data.get("package_id")
                total = validate_selection(payload, ids, package_id, checked=action == "approval")
                if action == "approval":
                    if workspace.stage != "awaiting_approval" or not publication.final:
                        fail("Оператор ещё не подготовил итоговые условия", "FINAL_NOT_READY", 409)
                    if set(ids) != set(
                        workspace.selection.get("option_ids", [])
                    ) or package_id != workspace.selection.get("package_id"):
                        fail(
                            "Выбор изменился; отправьте его оператору для проверки", "SELECTION_CHANGED", 409
                        )
                workspace.selection = {
                    "option_ids": ids,
                    "package_id": package_id,
                    "total": total,
                    "currency": payload["currency"],
                    "comment": data["comment"],
                    "revision": workspace.revision,
                }
                data.update(workspace.selection)
                workspace.stage = "approved" if action == "approval" else "review"
            elif not data["comment"]:
                fail("Напишите комментарий или пожелания к замене")
            if action == "alternative":
                workspace.stage = "review"
            workspace.version += 1
            workspace.save()
            record(workspace, action, data, notify=True)
        return Response(serialize(workspace, public=True))


class QuoteCheckThrottle(PublicResponseThrottle):
    rate = "120/min"
    scope = "quote_checks"

    def get_rate(self):
        return self.rate


class PublicWorkspaceRefreshView(PublicWorkspaceBase):
    throttle_classes = [QuoteCheckThrottle]

    def post(self, request, token):
        workspace = self.workspace(token)
        ensure_current(workspace, request.data)
        return Response(refresh_option(workspace, text(request.data.get("option_id", ""), 80)))


class WorkspaceServicesView(APIView):
    permission_classes = [require("offers.send", "orders.change")]

    def post(self, request, proposal_id):
        from decimal import Decimal

        from services.models import OrderService, ServiceOffer

        if not all(
            has_permission(request.user, permission) for permission in ("offers.send", "orders.change")
        ):
            fail("Нужны права отправки КП и изменения заказа", "PERMISSION_DENIED", 403)
        proposal = _get_proposal(request, proposal_id)
        with transaction.atomic():
            workspace = ProposalWorkspace.objects.select_for_update().get(pk=workspace_for(proposal).pk)
            check_version(workspace, request.data.get("version"))
            if workspace.stage != "approved" or not proposal.order_id:
                fail("Нужно согласованное КП, связанное с заказом", "APPROVAL_REQUIRED", 409)
            publication = ensure_active(workspace)
            payload = effective(workspace, publication)
            selection = workspace.selection
            validate_selection(
                payload, selection.get("option_ids", []), selection.get("package_id"), checked=True
            )
            bound = {}
            for event in workspace.interactions.filter(action="services_prepared").order_by("id"):
                bound.update(event.data.get("services", {}))
            selected = all_options(payload)
            result = {}
            for oid in selection["option_ids"]:
                row = selected[oid]
                service_id = row.get("service") or bound.get(oid)
                if service_id:
                    service = (
                        OrderService.objects.select_for_update()
                        .filter(pk=service_id, tenant_id=proposal.tenant_id, order_id=proposal.order_id)
                        .first()
                    )
                    if service is None or service.status not in {
                        "proposed",
                        "approval",
                        "booked",
                        "confirmed",
                        "issued",
                    }:
                        fail("Связанная услуга недоступна")
                    if service.status not in {"proposed", "approval"} and (
                        service.client_total != Decimal(row["price"])
                        or service.currency != payload["currency"]
                    ):
                        fail(
                            "Цена уже забронированной услуги изменилась; требуется отдельная обработка",
                            "BOOKED_SERVICE_CHANGED",
                            409,
                        )
                    if service.status in {"proposed", "approval"}:
                        service.client_total = Decimal(row["price"])
                        service.currency = payload["currency"]
                        service.version += 1
                        service.save(update_fields=["client_total", "currency", "version", "updated_at"])
                else:
                    offer = (
                        ServiceOffer.objects.filter(pk=row.get("offer"), tenant_id=proposal.tenant_id).first()
                        if row.get("offer")
                        else None
                    )
                    service = OrderService.objects.create(
                        tenant_id=proposal.tenant_id,
                        order=proposal.order,
                        kind=row["kind"],
                        title=row["title"],
                        source="api" if offer and not offer.is_manual else "manual",
                        status="proposed",
                        supplier=offer.supplier if offer else None,
                        provider_snapshot=offer.raw_snapshot if offer else {},
                        currency=payload["currency"],
                        client_total=Decimal(row["price"]),
                        supplier_cost=Decimal(
                            str((offer.raw_snapshot or {}).get("price", {}).get("amount", 0))
                        )
                        if offer
                        else Decimal(0),
                        created_by=request.user,
                    )
                result[oid] = str(service.pk)
            workspace.version += 1
            workspace.save()
            record(workspace, "services_prepared", {"services": result}, actor=request.user)
        return Response({**serialize(workspace), "prepared_services": result})


class WorkspaceRestoreDraftView(APIView):
    permission_classes = [require("offers.change")]

    def post(self, request, proposal_id, draft_version):
        proposal = _get_proposal(request, proposal_id)
        with transaction.atomic():
            workspace = ProposalWorkspace.objects.select_for_update().get(pk=workspace_for(proposal).pk)
            check_version(workspace, request.data.get("version"))
            source = workspace.draft_revisions.filter(version=draft_version).first()
            if not source:
                fail("Версия черновика не найдена", "NOT_FOUND", 404)
            ProposalDraftRevision.objects.get_or_create(
                workspace=workspace,
                version=workspace.version,
                defaults={"payload": workspace.draft, "created_by": request.user},
            )
            workspace.draft = normalize(
                source.payload, proposal, previous=workspace.draft, allow_incomplete=True
            )
            for row in all_options(workspace.draft).values():
                row["checked_at"] = None
            workspace.version += 1
            workspace.save()
            ProposalDraftRevision.objects.create(
                workspace=workspace,
                version=workspace.version,
                payload=workspace.draft,
                created_by=request.user,
            )
            record(workspace, "draft_restored", {"source_version": draft_version}, actor=request.user)
        return Response(serialize(workspace))
