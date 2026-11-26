from datetime import timedelta

import pytest
from django.utils import timezone

from common.drafts import check_draft_reminders
from common.models import WorkspaceDraft
from conftest import auth_client, make_user
from notifications.models import Notification

pytestmark = pytest.mark.django_db


def payload(key="free-booking"):
    now = timezone.now()
    return {
        "key": key,
        "meta": {
            "title": "Подбор в Стамбул",
            "category": "services",
            "client": "Иванов",
            "navigation": {"type": "free-booking", "route": "dashboard", "scope": "free-booking"},
            "services": ["Авиа"],
            "stage": "results",
        },
        "records": [
            {
                "scope": "free-booking/DetailedSearchPanel",
                "fields": {"params": {"from": "MOW", "to": "IST"}},
                "savedAt": int(now.timestamp() * 1000),
            }
        ],
        "created_at": (now - timedelta(hours=2)).isoformat(),
        "updated_at": now.isoformat(),
    }


def test_sync_dates_privacy_restore_and_idempotent_reminder(admin_client, admin_user, tenant, other_tenant):
    data = payload()
    response = admin_client.put("/api/v1/work-drafts/", data, format="json")
    assert response.status_code == 200, response.json()
    row = response.json()
    assert row["client"] == "Иванов" and row["responsible_id"] == str(admin_user.pk)
    assert row["created_at"] != row["updated_at"]
    same = admin_client.put("/api/v1/work-drafts/", data, format="json").json()
    assert same["version"] == row["version"]
    detail = admin_client.get(f"/api/v1/work-drafts/{row['id']}/").json()
    assert detail["records"][0]["fields"]["params"]["from"] == "MOW"
    stranger = make_user(other_tenant, "stranger@other.local", "admin")
    assert auth_client(stranger).get(f"/api/v1/work-drafts/{row['id']}/").status_code == 404
    scheduled = admin_client.patch(
        f"/api/v1/work-drafts/{row['id']}/",
        {"version": row["version"], "remind_at": (timezone.now() - timedelta(minutes=1)).isoformat()},
        format="json",
    )
    assert scheduled.status_code == 200 and scheduled.json()["overdue"]
    check_draft_reminders()
    check_draft_reminders()
    assert Notification.objects.filter(user=admin_user, event_type="draft.reminder").count() == 1
    assert (
        admin_client.patch(
            f"/api/v1/work-drafts/{row['id']}/", {"version": row["version"], "remind_at": None}, format="json"
        ).status_code
        == 409
    )
    sent = admin_client.patch(
        f"/api/v1/work-drafts/{row['id']}/",
        {"version": scheduled.json()["version"], "status": "sent"},
        format="json",
    )
    assert sent.status_code == 200
    assert admin_client.get("/api/v1/work-drafts/").json()["results"] == []
    check_draft_reminders()
    assert Notification.objects.filter(event_type="draft.reminder").count() == 1


def test_reminder_reassignment_is_checked_and_deduplicated(
    admin_client, admin_user, operator_client, operator_user, other_tenant
):
    row = operator_client.put("/api/v1/work-drafts/", payload(), format="json").json()
    assert (
        operator_client.patch(
            f"/api/v1/work-drafts/{row['id']}/",
            {"version": row["version"], "responsible_id": str(admin_user.pk)},
            format="json",
        ).status_code
        == 403
    )
    assert admin_client.get(f"/api/v1/work-drafts/{row['id']}/").status_code == 404
    # An administrator can reassign their own draft to an operator.
    own = admin_client.put("/api/v1/work-drafts/", payload("local-admin"), format="json").json()
    response = admin_client.patch(
        f"/api/v1/work-drafts/{own['id']}/",
        {
            "version": own["version"],
            "responsible_id": str(operator_user.pk),
            "remind_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
        },
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert any(d["id"] == own["id"] for d in operator_client.get("/api/v1/work-drafts/").json()["results"])
    check_draft_reminders()
    assert Notification.objects.filter(event_type="draft.reminder", user=operator_user).count() == 1


def test_source_registry_cannot_be_forged(admin_client):
    for key in ["kp/fake", "card/fake", "receipt/fake"]:
        assert admin_client.put("/api/v1/work-drafts/", payload(key), format="json").status_code == 400
    invalid = payload()
    invalid["records"] = [{"scope": "x", "fields": []}]
    assert admin_client.put("/api/v1/work-drafts/", invalid, format="json").status_code == 400


def test_malformed_meta_and_responsible_are_validation_errors(admin_client):
    invalid = payload()
    invalid["meta"] = []
    assert admin_client.put("/api/v1/work-drafts/", invalid, format="json").status_code == 400
    row = admin_client.put("/api/v1/work-drafts/", payload(), format="json").json()
    assert (
        admin_client.patch(
            f"/api/v1/work-drafts/{row['id']}/",
            {"version": row["version"], "responsible_id": "invalid"},
            format="json",
        ).status_code
        == 400
    )


def test_published_proposals_do_not_leave_stale_reminders(admin_client, admin_user, tenant):
    from common.drafts import source_is_pending
    from offers.models import Proposal, ProposalPublication, ProposalWorkspace

    proposal = Proposal.objects.create(tenant=tenant, created_by=admin_user, number="KP-REGISTRY")
    workspace = ProposalWorkspace.objects.create(proposal=proposal, draft={"title": "Проверка"})
    admin_client.get("/api/v1/work-drafts/")
    row = WorkspaceDraft.objects.get(key=f"kp/{proposal.pk}")
    row.remind_at = timezone.now() - timedelta(minutes=1)
    row.save()
    assert source_is_pending(row)
    ProposalPublication.objects.create(
        workspace=workspace, revision=workspace.revision, payload=workspace.draft
    )
    check_draft_reminders()
    assert not Notification.objects.filter(event_type="draft.reminder").exists()
    assert not any(d["id"] == str(row.pk) for d in admin_client.get("/api/v1/work-drafts/").json()["results"])
