"""Unified, private work registry. Reminder deadlines never dispatch a proposal."""

import hashlib
import json

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission
from common.errors import ApiError
from common.locking import check_version
from common.models import WorkspaceDraft
from common.outbox import emit_event
from common.scheduled import scheduled_task


def visible(user):
    return WorkspaceDraft.objects.filter(tenant_id=user.tenant_id, archived_at__isnull=True).filter(
        Q(owner=user) | Q(responsible=user)
    )


def source_is_pending(row):
    """Read authoritative state even if no browser has refreshed the registry."""
    from documents.models import Document
    from offers.models import ProposalWorkspace, ServiceCard

    ident = row.key.split("/", 1)[-1]
    if row.source == "proposal":
        ws = ProposalWorkspace.objects.filter(
            proposal_id=ident, proposal__tenant_id=row.tenant_id, proposal__archived_at__isnull=True
        ).first()
        pub = ws.publications.filter(revision=ws.revision).first() if ws else None
        return bool(ws and (not pub or ws.draft != pub.payload))
    if row.source == "receipt":
        return Document.objects.filter(
            pk=ident,
            tenant_id=row.tenant_id,
            archived_at__isnull=True,
            metadata__receipt_import__stage="draft",
        ).exists()
    if row.source == "card":
        return ServiceCard.objects.filter(
            pk=ident, tenant_id=row.tenant_id, archived_at__isnull=True, status="created"
        ).exists()
    return row.meta.get("status") == "unsent"


def refresh_sources(user):
    from documents.models import Document
    from offers.models import ProposalWorkspace, ServiceCard

    sources = []
    if has_permission(user, "offers.view"):
        for ws in (
            ProposalWorkspace.objects.filter(
                proposal__tenant_id=user.tenant_id, proposal__archived_at__isnull=True
            )
            .filter(Q(proposal__created_by=user) | Q(proposal__order__operator=user))
            .select_related("proposal__order", "proposal__created_by")
        ):
            p = ws.proposal
            pub = ws.publications.filter(revision=ws.revision).first()
            pending = not pub or ws.draft != pub.payload
            sources.append(
                (
                    f"kp/{p.pk}",
                    p,
                    "proposal",
                    {
                        "title": ws.draft.get("title") or p.number,
                        "category": "proposals",
                        "client": ws.draft.get("recipient") or p.recipient,
                        "number": p.number,
                        "status": "unsent" if pending else "sent",
                        "stage": ws.stage,
                        "services": list(
                            {
                                o.get("kind", "other")
                                for g in ws.draft.get("groups", [])
                                for o in g.get("options", [])
                            }
                        ),
                        "navigation": {"proposalId": str(p.pk), "type": "route", "route": "offers"},
                    },
                    ws.updated_at,
                )
            )
    if has_permission(user, "documents.view"):
        for d in (
            Document.objects.filter(
                tenant_id=user.tenant_id, archived_at__isnull=True, metadata__receipt_import__stage="draft"
            )
            .filter(Q(created_by=user) | Q(order__operator=user))
            .select_related("order")
        ):
            sources.append(
                (
                    f"receipt/{d.pk}",
                    d,
                    "receipt",
                    {
                        "title": d.title or "Квитанция",
                        "category": "receipts",
                        "status": "unsent",
                        "client": "",
                        "services": ["Редактор квитанций"],
                        "navigation": {"documentId": str(d.pk), "type": "receipt-draft", "route": "receipts"},
                    },
                    d.updated_at,
                )
            )
    if has_permission(user, "offers.view"):
        for c in (
            ServiceCard.objects.filter(tenant_id=user.tenant_id, archived_at__isnull=True)
            .filter(Q(created_by=user) | Q(order__operator=user))
            .select_related("order")
        ):
            sources.append(
                (
                    f"card/{c.pk}",
                    c,
                    "card",
                    {
                        "title": c.content.get("title") or "Карточка услуги",
                        "category": "services",
                        "status": "unsent" if c.status == "created" else "sent",
                        "client": "",
                        "services": [c.kind],
                        "navigation": {"type": "order", "route": "orders", "orderId": str(c.order_id)}
                        if c.order_id
                        else {"type": "route", "route": "services"},
                    },
                    c.updated_at,
                )
            )
    for key, obj, source, meta, edited in sources:
        order = obj.order if obj.order_id else None
        owner = obj.created_by or user
        row, _ = WorkspaceDraft.objects.get_or_create(
            tenant_id=user.tenant_id,
            owner=owner,
            key=key,
            defaults={
                "responsible": order.operator if order and order.operator_id else owner,
                "source": source,
                "created_by": owner,
                "created_at_source": obj.created_at,
            },
        )
        if order and not meta.get("client"):
            meta["client"] = (
                order.client_person.full_name
                if order.client_person_id
                else (order.client_company.short_name or order.client_company.legal_name)
                if order.client_company_id
                else ""
            )
        meta.update(order_id=str(order.pk) if order else "", order_number=order.number if order else "")
        if row.meta != meta or row.last_edited_at != edited:
            row.meta = meta
            row.last_edited_at = edited
            row.version += 1
            row.save()


def present(row):
    return {
        **row.meta,
        "id": str(row.pk),
        "key": row.key,
        "source": row.source,
        "version": row.version,
        "created_at": row.created_at_source.isoformat(),
        "updated_at": row.last_edited_at.isoformat(),
        "remind_at": row.remind_at.isoformat() if row.remind_at else None,
        "responsible_id": str(row.responsible_id),
        "responsible_name": row.responsible.get_full_name() or row.responsible.email,
        "overdue": bool(
            row.remind_at and row.remind_at < timezone.now() and row.meta.get("status") == "unsent"
        ),
    }


class LocalDraftSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=255)
    meta = serializers.JSONField()
    records = serializers.ListField(child=serializers.JSONField(), max_length=100)
    created_at = serializers.DateTimeField(required=False)
    updated_at = serializers.DateTimeField()

    def validate(self, data):
        if data["key"].startswith(("kp/", "receipt/", "card/")):
            raise serializers.ValidationError("Серверные черновики нельзя подменять локальными")
        if len(json.dumps(data, default=str)) > 4_000_000:
            raise serializers.ValidationError("Черновик слишком большой")
        if not isinstance(data["meta"], dict) or not isinstance(data["meta"].get("navigation"), dict):
            raise serializers.ValidationError("Некорректное описание черновика")
        if data.get("created_at", data["updated_at"]) > data["updated_at"]:
            raise serializers.ValidationError("Дата создания позже редактирования")
        if data["updated_at"] > timezone.now() + timezone.timedelta(minutes=2):
            raise serializers.ValidationError("Некорректное время редактирования")
        for record in data["records"]:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("scope"), str)
                or not isinstance(record.get("fields"), dict)
            ):
                raise serializers.ValidationError("Некорректные данные формы")
        return data


class DraftListView(APIView):
    def get(self, request):
        refresh_sources(request.user)
        check_draft_reminders(user=request.user)
        rows = visible(request.user).select_related("responsible").order_by("-last_edited_at")
        return Response(
            {
                "results": [
                    present(row)
                    for row in rows
                    if row.meta.get("status") == "unsent" and source_is_pending(row)
                ]
            }
        )

    @transaction.atomic
    def put(self, request):
        s = LocalDraftSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        row, created = WorkspaceDraft.objects.select_for_update().get_or_create(
            tenant_id=request.user.tenant_id,
            owner=request.user,
            key=d["key"],
            defaults={
                "responsible": request.user,
                "source": "local",
                "created_by": request.user,
                "created_at_source": d.get("created_at", d["updated_at"]),
            },
        )
        if row.source != "local":
            raise ApiError(code="VALIDATION_ERROR", message="Неверный источник", status_code=400)
        if created or d["updated_at"] > row.last_edited_at:
            row.meta = {**d["meta"], "status": "unsent"}
            row.records = d["records"]
            row.last_edited_at = d["updated_at"]
            row.archived_at = None
            row.version += 1
            row.save()
        return Response(present(row))


class DraftDetailView(APIView):
    def row(self, request, pk, lock=False):
        qs = visible(request.user)
        if lock:
            qs = qs.select_for_update()
        row = qs.filter(pk=pk).first()
        if not row:
            raise ApiError(code="NOT_FOUND", message="Черновик недоступен", status_code=404)
        return row

    def get(self, request, pk):
        row = self.row(request, pk)
        return Response({**present(row), "records": row.records})

    @transaction.atomic
    def patch(self, request, pk):
        row = self.row(request, pk, True)
        check_version(row, request.data.get("version"))
        if "remind_at" in request.data:
            field = serializers.DateTimeField(allow_null=True)
            row.remind_at = field.run_validation(request.data["remind_at"])
        if "responsible_id" in request.data:
            from accounts.models import User

            if not has_permission(request.user, "users.manage"):
                raise ApiError(
                    code="PERMISSION_DENIED",
                    message="Назначение ответственного доступно руководителю",
                    status_code=403,
                )
            row.responsible = User.objects.filter(
                tenant_id=request.user.tenant_id,
                pk=serializers.UUIDField().run_validation(request.data["responsible_id"]),
                status="active",
            ).first()
            if not row.responsible:
                raise serializers.ValidationError("Ответственный недоступен")
        if request.data.get("status") == "sent" and row.source == "local":
            row.meta = {**row.meta, "status": "sent"}
        row.version += 1
        row.updated_by = request.user
        row.save()
        return Response(present(row))

    @transaction.atomic
    def delete(self, request, pk):
        row = self.row(request, pk, True)
        if row.source != "local":
            raise serializers.ValidationError("Удалите черновик в его разделе")
        row.archived_at = timezone.now()
        row.save()
        return Response(status=204)


@scheduled_task("drafts.reminders")
def check_draft_reminders(user=None):
    from notifications.models import DeadlineThreshold, Notification

    qs = WorkspaceDraft.all_objects.filter(
        archived_at__isnull=True, remind_at__lte=timezone.now()
    ).select_related("responsible")
    if user:
        qs = qs.filter(tenant_id=user.tenant_id, responsible=user)
    count = 0
    for row in qs:
        if row.meta.get("status") != "unsent" or not row.responsible.is_active:
            continue
        if not source_is_pending(row):
            continue
        token = hashlib.sha256(row.remind_at.isoformat().encode()).hexdigest()[:24]
        with transaction.atomic():
            _, created = DeadlineThreshold.objects.get_or_create(
                rule_key="draft.reminder",
                resource_type="WorkspaceDraft",
                resource_id=str(row.pk),
                threshold=token,
                recipient=row.responsible,
            )
            if not created:
                continue
            note = Notification.objects.create(
                tenant_id=row.tenant_id,
                user=row.responsible,
                source="system",
                priority="high",
                event_type="draft.reminder",
                title="Просрочен срок отправки черновика",
                body=f"{row.meta.get('title', 'Черновик')} · {row.meta.get('client', '')} · {row.meta.get('order_number', '')} — откройте черновик на главной странице",
                resource_type="WorkspaceDraft",
                resource_id=str(row.pk),
                deep_link="/dashboard",
            )
            emit_event("notification.created", note, audience_user=row.responsible, tenant_id=row.tenant_id)
            count += 1
    return f"created {count} draft reminders"
