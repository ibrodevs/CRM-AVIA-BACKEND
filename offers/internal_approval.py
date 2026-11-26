"""Version-specific internal approval; never replaces the customer's confirmation."""

import hashlib
import json

from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import has_permission, require
from common.locking import check_version
from common.models import WorkspaceSetting
from common.outbox import emit_event
from notifications.models import Notification
from offers.models import ProposalInternalApproval, ProposalWorkspace
from offers.views import _get_proposal
from offers.workspace import fail, record, workspace_for


def fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def rules(workspace, user):
    setting = WorkspaceSetting.objects.filter(
        tenant_id=user.tenant_id, owner__isnull=True, namespace="proposal-approval"
    ).first()
    config = setting.value if setting else {}
    chain = (workspace.draft.get("policy_snapshot") or {}).get("approver_chain") or config.get(
        "approvers", []
    )
    roles = (
        list(user.roles.values_list("code", flat=True))
        if hasattr(user, "roles")
        else list(user.user_roles.values_list("role__code", flat=True))
    )
    author = workspace.proposal.created_by
    if author:
        roles = set(roles) | set(author.user_roles.values_list("role__code", flat=True))
    needed = bool(
        (workspace.draft.get("policy_snapshot") or {}).get("approver_chain")
        or config.get("required")
        or set(config.get("required_roles", [])) & set(roles)
        or not has_permission(user, "offers.send")
        or (author and not has_permission(author, "offers.send"))
    )
    return needed, chain


def current_approval(workspace):
    return (
        workspace.internal_approvals.filter(fingerprint=fingerprint(workspace.draft), status="approved")
        .order_by("-created_at")
        .first()
    )


def require_internal(workspace, user):
    if rules(workspace, user)[0] and not current_approval(workspace):
        fail(
            "Эта редакция КП требует внутреннего согласования перед отправкой",
            "INTERNAL_APPROVAL_REQUIRED",
            409,
        )


def summary(workspace, user=None):
    latest = (
        workspace.internal_approvals.filter(fingerprint=fingerprint(workspace.draft))
        .order_by("-created_at")
        .first()
    )
    return {
        "required": rules(workspace, user)[0] if user else False,
        "state": latest.status if latest else "not_requested",
        "id": str(latest.pk) if latest else None,
        "decisions": latest.decisions if latest else [],
        "step": latest.step if latest else 0,
        "can_decide": bool(
            user
            and latest
            and latest.status == "pending"
            and latest.step < len(latest.steps)
            and str(user.pk) in latest.steps[latest.step]
            and has_permission(user, "offers.approve")
        ),
    }


def notify_step(approval):
    for uid in approval.steps[approval.step]:
        user = User.objects.get(pk=uid)
        n = Notification.objects.create(
            tenant_id=user.tenant_id,
            user=user,
            source="offers",
            priority="medium",
            event_type="proposal.internal_approval",
            title="КП ожидает внутреннего согласования",
            body=f"{approval.workspace.proposal.number} · {approval.payload.get('title', '')}",
            resource_type="Proposal",
            resource_id=str(approval.workspace.proposal_id),
        )
        emit_event("notification.created", n, audience_user=user, tenant_id=user.tenant_id)


class InternalApprovalView(APIView):
    permission_classes = [require("offers.change", "offers.approve")]

    @transaction.atomic
    def post(self, request, proposal_id):
        p = _get_proposal(request, proposal_id)
        ws = ProposalWorkspace.objects.select_for_update().get(pk=workspace_for(p).pk)
        check_version(ws, request.data.get("version"))
        action = request.data.get("action", "request")
        if action == "request":
            if not has_permission(request.user, "offers.change"):
                fail("Нет права изменять КП", "PERMISSION_DENIED", 403)
            needed, chain = rules(ws, request.user)
            if not needed:
                fail("Для этой редакции внутреннее согласование не требуется")
            digest = fingerprint(ws.draft)
            approval = ws.internal_approvals.filter(
                fingerprint=digest, status__in=["pending", "approved"]
            ).first()
            if not approval:
                candidates = User.objects.filter(tenant_id=p.tenant_id, status="active").exclude(
                    pk=request.user.pk
                )
                eligible = [u for u in candidates if has_permission(u, "offers.approve")]
                steps = []
                for item in chain or ["*"]:
                    code = str(item).strip()
                    ids = [
                        str(u.pk)
                        for u in eligible
                        if code == "*" or code == str(u.pk) or u.user_roles.filter(role__code=code).exists()
                    ]
                    if not ids:
                        fail(f"Для этапа «{code}» нет доступного согласующего с правом offers.approve")
                    steps.append(ids)
                approval = ProposalInternalApproval.objects.create(
                    workspace=ws, fingerprint=digest, payload=ws.draft, steps=steps, requested_by=request.user
                )
                notify_step(approval)
                record(ws, "internal_requested", {"approval": str(approval.pk)}, actor=request.user)
        elif action in ("approve", "reject"):
            if not has_permission(request.user, "offers.approve"):
                fail("Нет права согласования КП", "PERMISSION_DENIED", 403)
            approval = (
                ws.internal_approvals.select_for_update()
                .filter(
                    pk=request.data.get("approval_id"), fingerprint=fingerprint(ws.draft), status="pending"
                )
                .first()
            )
            if not approval:
                fail("Редакция изменилась или согласование уже завершено", "STALE_APPROVAL", 409)
            if str(request.user.pk) not in approval.steps[approval.step]:
                fail("Вы не согласующий текущего этапа", "PERMISSION_DENIED", 403)
            comment = str(request.data.get("comment", "")).strip()[:4000]
            if action == "reject" and not comment:
                fail("Укажите причину отклонения")
            from django.utils import timezone

            approval.decisions.append(
                {
                    "user": str(request.user.pk),
                    "name": request.user.get_full_name(),
                    "at": timezone.now().isoformat(),
                    "step": approval.step,
                    "action": action,
                    "comment": comment,
                }
            )
            approval.step += 1
            approval.status = (
                "rejected"
                if action == "reject"
                else "approved"
                if approval.step == len(approval.steps)
                else "pending"
            )
            approval.save()
            if approval.status == "pending":
                notify_step(approval)
            else:
                author = approval.requested_by
                note = Notification.objects.create(
                    tenant_id=p.tenant_id,
                    user=author,
                    source="offers",
                    priority="medium",
                    event_type="proposal.internal_decision",
                    title="Редакция КП согласована"
                    if approval.status == "approved"
                    else "Редакция КП отклонена",
                    body=f"{p.number} · {comment}",
                    resource_type="Proposal",
                    resource_id=str(p.pk),
                )
                emit_event("notification.created", note, audience_user=author, tenant_id=p.tenant_id)
            record(
                ws,
                "internal_" + action,
                {"approval": str(approval.pk), "comment": comment},
                actor=request.user,
            )
        else:
            fail("Неизвестное действие согласования")
        ws.version += 1
        ws.save()
        from offers.workspace import serialize

        return Response(serialize(ws, user=request.user))
