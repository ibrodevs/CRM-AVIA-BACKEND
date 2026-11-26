# ruff: noqa: F811
import copy

import pytest

from common.models import WorkspaceSetting
from conftest import auth_client
from offers.models import ProposalInternalApproval, ProposalPublication, ProposalWorkspace
from offers.tests.test_proposals import order  # noqa:F401
from offers.tests.test_workspace import prepared  # noqa: F401

pytestmark = pytest.mark.django_db


def setting(tenant, value):
    return WorkspaceSetting.objects.create(
        tenant=tenant, namespace="proposal-approval", owner=None, value=value
    )


def test_matrix_is_optional_and_roles_control_requirement(
    admin_client, admin_user, operator_user, tenant, prepared
):  # noqa: F811
    path, value, _ = prepared
    setting(tenant, {"required": False, "required_roles": ["operator"], "approvers": ["admin"]})
    from offers.internal_approval import rules

    ws = ProposalWorkspace.objects.get(proposal_id=path.split("/")[-1])
    assert rules(ws, admin_user)[0] is False
    assert rules(ws, operator_user)[0] is True


def test_internal_approval_is_per_revision_cannot_self_approve_and_old_history_survives(
    admin_client, admin_user, manager_user, tenant, prepared
):  # noqa: F811
    path, value, public_path = prepared
    setting(tenant, {"required": True, "approvers": ["manager"]})
    assert (
        admin_client.post(path + "/publish/", {"version": value["version"]}, format="json").status_code == 409
    )
    requested = admin_client.post(
        path + "/internal-approval/", {"version": value["version"], "action": "request"}, format="json"
    )
    assert requested.status_code == 200, requested.json()
    value = requested.json()
    approval = value["internal_approval"]
    assert approval["state"] == "pending"
    assert (
        admin_client.post(
            path + "/internal-approval/",
            {"version": value["version"], "action": "approve", "approval_id": approval["id"]},
            format="json",
        ).status_code
        == 403
    )
    manager = auth_client(manager_user)
    decision = manager.post(
        path + "/internal-approval/",
        {"version": value["version"], "action": "approve", "approval_id": approval["id"]},
        format="json",
    )
    assert decision.status_code == 200, decision.json()
    approved = decision.json()
    assert approved["internal_approval"]["state"] == "approved"
    published = admin_client.post(path + "/publish/", {"version": approved["version"]}, format="json")
    assert published.status_code == 200, published.json()
    before = ProposalPublication.objects.get(
        workspace__proposal_id=path.split("/")[-1], revision=published.json()["revision"]
    ).payload
    changed = copy.deepcopy(published.json()["draft"])
    changed["groups"][0]["options"][0]["price"] = "150"
    saved = admin_client.put(
        path + "/workspace/", {"version": published.json()["version"], "draft": changed}, format="json"
    ).json()
    assert saved["internal_approval"]["state"] == "not_requested"
    assert (
        admin_client.post(path + "/publish/", {"version": saved["version"]}, format="json").status_code == 409
    )
    assert ProposalInternalApproval.objects.get(pk=approval["id"]).status == "approved"
    from rest_framework.test import APIClient

    public = APIClient().get(public_path + "/").json()
    assert "internal_approval" not in public
    assert not any(e["action"].startswith("internal_") for e in public["events"])
    assert (
        ProposalPublication.objects.get(
            workspace__proposal_id=path.split("/")[-1], revision=published.json()["revision"]
        ).payload
        == before
    )


def test_wrong_role_and_stale_decision_are_rejected(admin_client, manager_user, tenant, prepared):  # noqa: F811
    path, value, _ = prepared
    setting(tenant, {"required": True, "approvers": ["manager"]})
    requested = admin_client.post(
        path + "/internal-approval/", {"version": value["version"]}, format="json"
    ).json()
    changed = copy.deepcopy(requested["draft"])
    changed["title"] = "Новые условия"
    saved = admin_client.put(
        path + "/workspace/", {"version": requested["version"], "draft": changed}, format="json"
    ).json()
    response = auth_client(manager_user).post(
        path + "/internal-approval/",
        {
            "version": saved["version"],
            "action": "approve",
            "approval_id": requested["internal_approval"]["id"],
        },
        format="json",
    )
    assert response.status_code == 409


def test_changed_terms_cannot_be_booked_through_prepared_service(admin_client, prepared):  # noqa: F811
    from common.errors import ApiError
    from offers.booking_guard import require_proposal_approval
    from offers.tests.test_workspace import post
    from services.models import OrderService

    path, value, public = prepared
    chosen = post(public, value, "selection", option_ids=["a"]).json()
    final = admin_client.post(
        path + "/publish/", {"version": chosen["version"], "final": True}, format="json"
    ).json()
    approved = post(public, final, "approval", option_ids=["a"]).json()
    response = admin_client.post(path + "/prepare-services/", {"version": approved["version"]}, format="json")
    assert response.status_code == 200, response.json()
    service = OrderService.objects.get()
    require_proposal_approval(service)
    changed = copy.deepcopy(response.json()["draft"])
    changed["groups"][0]["options"][0]["price"] = "500"
    saved = admin_client.put(
        path + "/workspace/", {"version": response.json()["version"], "draft": changed}, format="json"
    )
    assert saved.status_code == 200
    with pytest.raises(ApiError) as err:
        require_proposal_approval(service)
    assert err.value.code == "PROPOSAL_APPROVAL_REQUIRED"
    blocked = admin_client.post(
        path + "/prepare-services/", {"version": saved.json()["version"]}, format="json"
    )
    assert blocked.status_code == 409
    # Existing services outside КП retain their ordinary workflow.
    service.provider_snapshot = {}
    require_proposal_approval(service)


def test_travel_policy_chain_overrides_optional_setting(
    admin_client, admin_user, manager_user, tenant, prepared
):  # noqa: F811
    from offers.internal_approval import rules

    path, value, _ = prepared
    setting(tenant, {"required": False, "approvers": ["admin"]})
    draft = copy.deepcopy(value["draft"])
    from crm.models import Company
    from travel_policy.models import TravelPolicy

    company = Company.objects.create(tenant=tenant, legal_name="Компания")
    policy = TravelPolicy.objects.create(
        tenant=tenant, company=company, name="Политика", approver_chain=["manager"]
    )
    ws = ProposalWorkspace.objects.get(proposal_id=path.split("/")[-1])
    ws.proposal.order.client_company = company
    ws.proposal.order.client_person = None
    ws.proposal.order.save(update_fields=["client_company", "client_person"])
    draft["policy_id"] = str(policy.pk)
    saved = admin_client.put(
        path + "/workspace/", {"version": value["version"], "draft": draft}, format="json"
    )
    assert saved.status_code == 200, saved.json()
    ws = ProposalWorkspace.objects.get(proposal_id=path.split("/")[-1])
    assert rules(ws, admin_user) == (True, ["manager"])
    requested = admin_client.post(
        path + "/internal-approval/", {"version": saved.json()["version"]}, format="json"
    ).json()
    response = auth_client(manager_user).post(
        path + "/internal-approval/",
        {
            "version": requested["version"],
            "approval_id": requested["internal_approval"]["id"],
            "action": "reject",
            "comment": "Превышен бюджет",
        },
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert response.json()["internal_approval"]["state"] == "rejected"
    from notifications.models import Notification

    assert Notification.objects.filter(user=admin_user, event_type="proposal.internal_decision").exists()


def test_admin_publication_cannot_bypass_the_author_role_requirement(
    admin_client, admin_user, operator_user, tenant, prepared
):
    path, value, _ = prepared
    setting(tenant, {"required": False, "required_roles": ["operator"], "approvers": ["admin"]})
    ws = ProposalWorkspace.objects.get(proposal_id=path.split("/")[-1])
    ws.proposal.created_by = operator_user
    ws.proposal.save(update_fields=["created_by"])
    response = admin_client.post(path + "/publish/", {"version": value["version"]}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INTERNAL_APPROVAL_REQUIRED"
