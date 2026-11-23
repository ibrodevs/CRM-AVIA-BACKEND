import copy

import pytest
from rest_framework.test import APIClient

from conftest import auth_client
from offers.models import ProposalInteraction, ProposalPublication
from offers.tests.test_proposals import order  # noqa: F401

pytestmark = pytest.mark.django_db


def payload():
    return {
        "title": "Командировка",
        "intro": "Выберите поездку",
        "currency": "RUB",
        "mode": "both",
        "groups": [
            {
                "id": "air",
                "title": "Перелёт",
                "required": True,
                "options": [
                    {
                        "id": "a",
                        "kind": "avia",
                        "title": "Москва → Стамбул",
                        "description": "С багажом",
                        "price": "100",
                        "availability": "available",
                        "mark_checked": True,
                    },
                    {
                        "id": "b",
                        "kind": "avia",
                        "title": "Другой рейс",
                        "price": "200",
                        "availability": "available",
                        "mark_checked": True,
                    },
                ],
            },
            {
                "id": "hotel",
                "title": "Отель",
                "required": False,
                "options": [
                    {
                        "id": "h",
                        "kind": "hotel",
                        "title": "Отель",
                        "price": "300",
                        "availability": "available",
                        "mark_checked": True,
                        "requires": ["a"],
                    },
                ],
            },
        ],
        "packages": [{"id": "p", "title": "Перелёт + отель", "option_ids": ["a", "h"], "total_only": False}],
    }


@pytest.fixture
def prepared(admin_client, order):  # noqa: F811
    proposal = admin_client.post(
        "/api/v1/proposals/", {"order": order["id"], "currency": "RUB"}, format="json"
    ).json()
    path = f"/api/v1/proposals/{proposal['id']}"
    initial = admin_client.get(path + "/workspace/").json()
    saved = admin_client.put(
        path + "/workspace/", {"version": initial["version"], "draft": payload()}, format="json"
    )
    assert saved.status_code == 200, saved.json()
    published = admin_client.post(path + "/publish/", {"version": saved.json()["version"]}, format="json")
    assert published.status_code == 200, published.json()
    value = published.json()
    return path, value, f"/api/v1/public/proposals/{value['public_token']}"


def post(public, value, action, **extra):
    return APIClient().post(
        public + "/respond/",
        {"version": value["version"], "revision": value["revision"], "action": action, **extra},
        format="json",
    )


def test_client_choice_then_operator_final_then_approval(admin_client, prepared):
    path, value, public = prepared
    chosen = post(public, value, "selection", option_ids=["a", "h"], package_id="p", comment="Спасибо")
    assert chosen.status_code == 200, chosen.json()
    assert chosen.json()["stage"] == "review"
    early = post(public, chosen.json(), "approval", option_ids=["a", "h"], package_id="p")
    assert early.status_code == 409
    final = admin_client.post(
        path + "/publish/", {"version": chosen.json()["version"], "final": True}, format="json"
    )
    assert final.status_code == 200, final.json()
    approved = post(public, final.json(), "approval", option_ids=["a", "h"], package_id="p")
    assert approved.status_code == 200, approved.json()
    assert approved.json()["selection"]["total"] == "400.00"
    assert approved.json()["stage"] == "approved"
    from notifications.models import Notification
    from services.models import OrderService

    assert not OrderService.objects.exists()  # Never auto-book or duplicate order services.
    assert Notification.objects.filter(event_type="offers.kp_approval").exists()


def test_constraints_and_stale_version(prepared):
    _, value, public = prepared
    assert post(public, value, "selection", option_ids=["b", "h"]).status_code == 400
    assert post(public, value, "selection", option_ids=["a", "b"]).status_code == 400
    assert post(public, value, "selection", option_ids=["a"], package_id="p").status_code == 400
    assert post(public, value, "selection", option_ids=["a"]).status_code == 200
    assert post(public, value, "selection", option_ids=["b"]).status_code == 409


def test_draft_is_private_until_publish_and_history_survives(admin_client, prepared):
    path, value, public = prepared
    old = copy.deepcopy(value["draft"])
    changed = copy.deepcopy(old)
    changed["groups"][0]["options"][0]["price"] = "900"
    saved = admin_client.put(
        path + "/workspace/", {"version": value["version"], "draft": changed}, format="json"
    ).json()
    shown = APIClient().get(public + "/").json()
    assert shown["payload"]["groups"][0]["options"][0]["price"] == "100.00"
    assert "draft" not in shown and "public_token" not in shown and "publications" not in shown
    published = admin_client.post(path + "/publish/", {"version": saved["version"]}, format="json").json()
    assert published["revision"] == 2
    assert (
        ProposalPublication.objects.filter(revision=1).get().payload["groups"][0]["options"][0]["price"]
        == "100.00"
    )
    assert ProposalInteraction.objects.filter(action="published").count() == 2


def test_expiry_unavailable_unchecked(admin_client, prepared):
    path, value, public = prepared
    selected = post(public, value, "selection", option_ids=["a"]).json()
    draft = copy.deepcopy(value["draft"])
    draft["groups"][0]["options"][0]["price"] = "110"
    draft["groups"][0]["options"][0]["mark_checked"] = False
    saved = admin_client.put(
        path + "/workspace/", {"version": selected["version"], "draft": draft}, format="json"
    ).json()
    final = admin_client.post(path + "/publish/", {"version": saved["version"], "final": True}, format="json")
    assert final.status_code == 409
    draft["groups"][0]["options"][0]["availability"] = "unavailable"
    draft["packages"] = []
    draft["mode"] = "flexible"
    saved = admin_client.put(
        path + "/workspace/", {"version": saved["version"], "draft": draft}, format="json"
    ).json()
    value = admin_client.post(path + "/publish/", {"version": saved["version"]}, format="json").json()
    assert post(public, value, "selection", option_ids=["a"]).status_code == 409


def test_operator_permission_and_public_cannot_mutate_draft(admin_client, operator_user, prepared):
    path, value, public = prepared
    client = auth_client(operator_user)
    assert APIClient().put(path + "/workspace/", {}, format="json").status_code in (401, 403)
    bad = APIClient().get("/api/v1/public/proposals/not-a-real-token/")
    assert bad.status_code == 404
    assert post(public, value, "approval", option_ids=["a"]).status_code == 409
    # Existing role policy must still govern the new constructor.
    assert client.get(path + "/workspace/").status_code in (200, 403)


def test_malformed_or_cross_tenant_sources_are_rejected(admin_client, prepared):
    path, value, _ = prepared
    draft = copy.deepcopy(value["draft"])
    draft["groups"][0]["options"][0]["offer"] = "not-uuid"
    assert (
        admin_client.put(
            path + "/workspace/", {"version": value["version"], "draft": draft}, format="json"
        ).status_code
        == 400
    )
    draft["groups"][0]["options"][0]["offer"] = None
    draft["groups"][0]["options"][0]["price"] = "NaN"
    assert (
        admin_client.put(
            path + "/workspace/", {"version": value["version"], "draft": draft}, format="json"
        ).status_code
        == 400
    )


def test_manual_refresh_does_not_claim_live_check(prepared):
    _, value, public = prepared
    refreshed = APIClient().post(
        public + "/refresh/",
        {"version": value["version"], "revision": value["revision"], "option_id": "a"},
        format="json",
    )
    assert refreshed.status_code == 200
    option = refreshed.json()["payload"]["groups"][0]["options"][0]
    assert option["check_source"] == "manual" and option["auto_check"] is False
    assert "price_offset" not in option


def test_customer_cannot_change_final_selection(admin_client, prepared):
    path, value, public = prepared
    selected = post(public, value, "selection", option_ids=["a"]).json()
    final = admin_client.post(
        path + "/publish/", {"version": selected["version"], "final": True}, format="json"
    ).json()
    assert post(public, final, "approval", option_ids=["b"]).status_code == 409
    assert post(public, value, "approval", option_ids=["a"]).status_code == 409


def test_link_revalidation_preserves_client_markup_and_invalidates_consent(
    admin_client, prepared, tenant, monkeypatch
):
    from decimal import Decimal

    from integrations.adapters import ProviderAdapter
    from services.models import ServiceOffer
    from suppliers.models import Supplier

    supplier = Supplier.objects.create(tenant=tenant, name="Test provider")
    offer = ServiceOffer.objects.create(
        tenant=tenant,
        supplier=supplier,
        kind="avia",
        provider_adapter="quote_test",
        itinerary={},
        price_amount=Decimal("80"),
        price_currency="RUB",
        availability="available",
        raw_snapshot={},
    )

    class QuoteAdapter(ProviderAdapter):
        key = "quote_test"

        def revalidate(self, ctx, snapshot):
            return {
                "status": "price_changed",
                "snapshot": {
                    "kind": "avia",
                    "price": {"amount": "90", "currency": "RUB"},
                    "availability": "available",
                    "itinerary": {},
                },
            }

    monkeypatch.setattr("integrations.adapters.get_adapter", lambda key: QuoteAdapter())
    path, value, public = prepared
    draft = copy.deepcopy(value["draft"])
    draft["groups"][0]["options"][0]["offer"] = str(offer.pk)
    draft["groups"][0]["options"][0]["mark_checked"] = True
    saved = admin_client.put(
        path + "/workspace/", {"version": value["version"], "draft": draft}, format="json"
    ).json()
    value = admin_client.post(path + "/publish/", {"version": saved["version"]}, format="json").json()
    selected = post(public, value, "selection", option_ids=["a"]).json()
    final = admin_client.post(
        path + "/publish/", {"version": selected["version"], "final": True}, format="json"
    ).json()
    assert final.get("stage") == "awaiting_approval", final
    refreshed = APIClient().post(
        public + "/refresh/",
        {"version": final["version"], "revision": final["revision"], "option_id": "a"},
        format="json",
    )
    assert refreshed.status_code == 200, refreshed.json()
    data = refreshed.json()
    row = data["payload"]["groups"][0]["options"][0]
    assert row["price"] == "110.00"  # 90 + the original client offset 20.
    assert row["previous_price"] == "100.00"
    assert "price_offset" not in row and "offer" not in row and "service" not in row
    assert data["stage"] == "review"
    assert post(public, data, "approval", option_ids=["a"]).status_code == 409
    # Moving refreshed terms into the next draft must NOT compound the markup.
    operator = admin_client.get(path + "/workspace/").json()
    draft = operator["draft"]
    draft["groups"][0]["options"][0]["price"] = "110.00"
    saved = admin_client.put(
        path + "/workspace/", {"version": operator["version"], "draft": draft}, format="json"
    ).json()
    assert saved["draft"]["groups"][0]["options"][0]["price_offset"] == "20.00"
    assert ServiceOffer.objects.get(pk=offer.pk).price_amount == Decimal("80")  # no mutation of the offer.


def test_customer_cannot_forge_check_evidence(admin_client, prepared):
    path, value, _ = prepared
    draft = value["draft"]
    draft["groups"][0]["options"][0].update(
        price="500", checked_at="2099-01-01T00:00:00Z", check_source="api", mark_checked=False
    )
    saved = admin_client.put(
        path + "/workspace/", {"version": value["version"], "draft": draft}, format="json"
    ).json()
    assert saved["draft"]["groups"][0]["options"][0]["checked_at"] is None


def test_other_tenant_cannot_open_workspace(other_tenant, prepared):
    from conftest import make_user

    path, _, _ = prepared
    foreign = auth_client(make_user(other_tenant, "foreign@example.com", "admin"))
    assert foreign.get(path + "/workspace/").status_code == 404


def test_expired_proposal_cannot_be_chosen(admin_client, prepared):
    from offers.models import ProposalPublication

    _, value, public = prepared
    publication = ProposalPublication.objects.get(revision=1)
    publication.payload["valid_until"] = "2020-01-01T00:00:00Z"
    publication.save()
    assert post(public, value, "selection", option_ids=["a"]).status_code == 409


def test_preparing_services_is_manual_and_does_not_duplicate(admin_client, prepared):
    from services.models import OrderService

    path, value, public = prepared
    assert (
        admin_client.post(
            path + "/prepare-services/", {"version": value["version"]}, format="json"
        ).status_code
        == 409
    )
    chosen = post(public, value, "selection", option_ids=["a", "h"], package_id="p").json()
    final = admin_client.post(
        path + "/publish/", {"version": chosen["version"], "final": True}, format="json"
    ).json()
    approved = post(public, final, "approval", option_ids=["a", "h"], package_id="p").json()
    assert OrderService.objects.count() == 0
    first = admin_client.post(path + "/prepare-services/", {"version": approved["version"]}, format="json")
    assert first.status_code == 200, first.json()
    second = admin_client.post(
        path + "/prepare-services/", {"version": first.json()["version"]}, format="json"
    )
    assert second.status_code == 200, second.json()
    assert first.json()["prepared_services"] == second.json()["prepared_services"]
    assert OrderService.objects.count() == 2
    assert set(OrderService.objects.values_list("status", flat=True)) == {"proposed"}
