"""A successful HTTP exchange is not proof of supplier issuance."""
# ruff: noqa: F811

from types import SimpleNamespace

import pytest

from avia.models import AviaBooking, Ticket
from booking.job_handlers import _save_tickets
from booking.tests.test_booking import TestBookingSaga as BookingScenario
from booking.tests.test_booking import ready_order, run_jobs_once  # noqa: F401
from integrations.adapters import MockAdapter

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("response", [{}, {"status": "pending"}, {"status": "booked"}])
def test_unconfirmed_issue_never_creates_tickets(admin_client, ready_order, monkeypatch, response):
    workflow = BookingScenario()._booked_workflow(admin_client, ready_order)
    monkeypatch.setattr(MockAdapter, "issue", lambda *args: response)
    result = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/issue/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"unconfirmed-{workflow['id']}",
    )
    assert result.status_code == 202
    run_jobs_once()
    item = admin_client.get(f"/api/v1/booking-workflows/{workflow['id']}/status/").json()["items"][0]
    assert item["status"] == "unknown"
    assert Ticket.objects.count() == 0


def test_transfer_status_does_not_create_an_aviation_booking():
    _save_tickets(None, SimpleNamespace(kind="transfer"), None, {"status": "issued"})
    assert AviaBooking.objects.count() == 0
