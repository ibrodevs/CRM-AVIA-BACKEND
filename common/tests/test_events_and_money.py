from decimal import Decimal

import pytest

from common.fields import decrypt_value, encrypt_value, mask_tail
from common.money import money_dict, quantize
from common.outbox import emit_event
from conftest import auth_client

pytestmark = pytest.mark.django_db


class TestEventFeed:
    def test_cursor_pagination(self, admin_user, tenant):
        for i in range(5):
            emit_event("order.updated", "Order", payload={"n": i}, tenant_id=tenant.id)
        client = auth_client(admin_user)
        body = client.get("/api/v1/events/?cursor=0&limit=3").json()
        assert len(body["events"]) == 3
        cursor = body["cursor"]
        body2 = client.get(f"/api/v1/events/?cursor={cursor}").json()
        assert len(body2["events"]) == 2
        assert body2["events"][0]["event_id"] > cursor

    def test_event_shape(self, admin_user, tenant):
        emit_event("chat.message.created", "Message", payload={"thread_id": "t1"}, tenant_id=tenant.id)
        client = auth_client(admin_user)
        event = client.get("/api/v1/events/?cursor=0").json()["events"][0]
        assert set(event) == {
            "event_id",
            "type",
            "occurred_at",
            "resource_type",
            "resource_id",
            "version",
            "payload",
        }

    def test_audience_user_filter(self, admin_user, operator_user, tenant):
        emit_event("notification.created", "Notification", audience_user=operator_user, tenant_id=tenant.id)
        admin_events = auth_client(admin_user).get("/api/v1/events/?cursor=0").json()["events"]
        operator_events = auth_client(operator_user).get("/api/v1/events/?cursor=0").json()["events"]
        assert admin_events == []
        assert len(operator_events) == 1

    def test_etag_304(self, admin_user, tenant):
        emit_event("order.updated", "Order", tenant_id=tenant.id)
        client = auth_client(admin_user)
        response = client.get("/api/v1/events/?cursor=0")
        etag = response["ETag"]
        cursor = response.json()["cursor"]
        response2 = client.get(f"/api/v1/events/?cursor={cursor}", HTTP_IF_NONE_MATCH=etag)
        assert response2.status_code == 304


class TestMoney:
    def test_quantize_half_up(self):
        assert quantize(Decimal("1.005"), "USD") == Decimal("1.01")
        assert quantize(Decimal("1.004"), "USD") == Decimal("1.00")

    def test_minor_units(self):
        assert str(quantize(Decimal("100.4"), "JPY")) == "100"
        assert str(quantize(Decimal("1.0005"), "KWD")) == "1.001"

    def test_float_forbidden(self):
        with pytest.raises(TypeError):
            quantize(1.5, "USD")  # type: ignore[arg-type]

    def test_money_dict_format(self):
        assert money_dict(Decimal("1720"), "USD") == {"amount": "1720.00", "currency": "USD"}


class TestFieldEncryption:
    def test_roundtrip(self):
        encrypted = encrypt_value("AC1234567")
        assert encrypted.startswith("enc$1$")
        assert "AC1234567" not in encrypted
        assert decrypt_value(encrypted) == "AC1234567"

    def test_mask(self):
        assert mask_tail("AC1234567") == "*****4567"
        assert mask_tail("") == ""
