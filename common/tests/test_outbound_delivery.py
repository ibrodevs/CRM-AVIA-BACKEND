"""Слой исходящей доставки: транспорты, очереди, честные ответы API."""

from decimal import Decimal

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from common.models import OutboundDelivery
from common.transports import ChannelNotConfigured, channel_status, is_configured, send_message
from conftest import auth_client

pytestmark = pytest.mark.django_db


UNCONFIGURED = {
    "EMAIL_HOST": "",
    "DEFAULT_FROM_EMAIL": "",
    "TELEGRAM_BOT_TOKEN": "",
    "WHATSAPP_API_URL": "",
    "WHATSAPP_API_TOKEN": "",
    "SMS_GATEWAY_URL": "",
    "SMS_GATEWAY_TOKEN": "",
}


class TestChannelStatus:
    def test_in_app_channels_are_always_available(self):
        status = channel_status()
        assert status["desktop"]["configured"] is True
        assert status["internal"]["configured"] is True

    @override_settings(**UNCONFIGURED)
    def test_external_channels_report_their_requirement(self):
        status = channel_status()
        assert status["email"]["configured"] is False
        assert "SMTP" in status["email"]["requirement"]
        assert status["telegram"]["configured"] is False
        assert status["push"]["configured"] is False

    @override_settings(**UNCONFIGURED)
    def test_unconfigured_channel_refuses_instead_of_pretending(self):
        with pytest.raises(ChannelNotConfigured):
            send_message("email", "client@example.com", subject="Тема", body="Текст")

    def test_email_is_configured_in_tests(self):
        assert is_configured("email") is True

    def test_channels_endpoint_lists_configured_and_unconfigured(self, admin_user):
        body = auth_client(admin_user).get("/api/v1/notification-channels/").json()
        names = {row["channel"] for row in body["channels"]}
        assert {"email", "telegram", "sms", "push", "desktop"} <= names
        assert "push" in body["unconfigured"]


class TestNotificationDeliveryQueue:
    def _queued_delivery(self, tenant, user, channel="email"):
        from notifications.models import Notification, NotificationDelivery

        notification = Notification.objects.create(
            tenant=tenant, user=user, title="Дедлайн выписки", body="Заказ ORD-1 · статус: В работе"
        )
        return NotificationDelivery.objects.create(notification=notification, channel=channel)

    def test_queued_email_is_actually_sent(self, tenant, operator_user):
        from notifications.scheduled_tasks import dispatch_deliveries

        delivery = self._queued_delivery(tenant, operator_user)
        mail.outbox.clear()
        dispatch_deliveries()

        delivery.refresh_from_db()
        assert delivery.state == "sent"
        assert delivery.sent_at is not None
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [operator_user.email]
        assert mail.outbox[0].subject == "Дедлайн выписки"

    @override_settings(**UNCONFIGURED)
    def test_unconfigured_channel_is_skipped_not_silently_sent(self, tenant, operator_user):
        from notifications.scheduled_tasks import dispatch_deliveries

        delivery = self._queued_delivery(tenant, operator_user, channel="telegram")
        dispatch_deliveries()

        delivery.refresh_from_db()
        assert delivery.state == "skipped"
        assert delivery.sent_at is None
        assert "не настроен" in delivery.error

    def test_missing_address_fails_instead_of_reporting_success(self, tenant, operator_user):
        from notifications.scheduled_tasks import dispatch_deliveries

        delivery = self._queued_delivery(tenant, operator_user, channel="telegram")
        dispatch_deliveries()

        delivery.refresh_from_db()
        assert delivery.state in ("failed", "skipped")
        assert delivery.sent_at is None

    def test_in_app_channel_needs_no_address(self, tenant, operator_user):
        from notifications.scheduled_tasks import dispatch_deliveries

        delivery = self._queued_delivery(tenant, operator_user, channel="desktop")
        dispatch_deliveries()

        delivery.refresh_from_db()
        assert delivery.state == "sent"

    def test_notification_body_is_human_readable(self, tenant, admin_user, operator_user):
        """Тело уведомления больше не Python-дамп события."""
        from common.models import OutboxEvent
        from notifications.messages import render_body

        event = OutboxEvent.objects.create(
            tenant=tenant,
            event_type="order.updated",
            resource_type="Order",
            resource_id="",
            payload={"action": "created", "status": "in_progress"},
        )
        body = render_body(event)
        assert "{" not in body
        assert "создан" in body
        assert "В работе" in body


class TestDocumentSend:
    def _document(self, tenant, user, with_version=True):
        from django.core.files.base import ContentFile

        from documents.models import Document, DocumentVersion

        document = Document.objects.create(
            tenant=tenant, kind="voucher", title="Ваучер №1", current_version=1 if with_version else 0
        )
        if with_version:
            DocumentVersion.objects.create(
                document=document,
                version=1,
                file=ContentFile(b"%PDF-1.4 test", name="voucher.pdf"),
                checksum_sha256="0" * 64,
                mime_type="application/pdf",
                size_bytes=13,
                original_name="voucher.pdf",
                scan_status="clean",
            )
        return document

    def test_send_creates_a_real_queue_entry(self, tenant, admin_user):
        document = self._document(tenant, admin_user)
        client = auth_client(admin_user)
        body = client.post(
            f"/api/v1/documents/{document.id}/send/",
            {"channel": "email", "recipient": "client@example.com"},
            format="json",
        ).json()

        assert body["status"] == "queued"
        assert body["channel_configured"] is True
        assert body["recipient"] == "client@example.com"
        delivery = OutboundDelivery.all_objects.get(pk=body["delivery"])
        assert delivery.state == "queued"
        assert delivery.document_id == document.id

    @override_settings(**UNCONFIGURED)
    def test_send_reports_unconfigured_channel(self, tenant, admin_user):
        document = self._document(tenant, admin_user)
        client = auth_client(admin_user)
        body = client.post(
            f"/api/v1/documents/{document.id}/send/",
            {"channel": "telegram", "recipient": "12345"},
            format="json",
        ).json()

        assert body["channel_configured"] is False
        assert "не настроен" in body["detail"]

    def test_dispatcher_sends_the_document_file(self, tenant, admin_user):
        from common.scheduled_tasks import dispatch_outbound_deliveries

        document = self._document(tenant, admin_user)
        auth_client(admin_user).post(
            f"/api/v1/documents/{document.id}/send/",
            {"channel": "email", "recipient": "client@example.com"},
            format="json",
        )
        mail.outbox.clear()
        dispatch_outbound_deliveries()

        delivery = OutboundDelivery.all_objects.filter(document=document).first()
        assert delivery.state == "sent"
        assert len(mail.outbox) == 1
        assert mail.outbox[0].attachments, "документ должен уходить вложением"

    def test_deliveries_endpoint_shows_state(self, tenant, admin_user):
        document = self._document(tenant, admin_user)
        client = auth_client(admin_user)
        client.post(
            f"/api/v1/documents/{document.id}/send/",
            {"channel": "email", "recipient": "client@example.com"},
            format="json",
        )
        rows = client.get(f"/api/v1/documents/{document.id}/deliveries/").json()
        assert len(rows) == 1
        assert rows[0]["state"] == "queued"
        assert rows[0]["recipient"] == "client@example.com"


class TestFinanceExports:
    def test_accounting_export_returns_a_real_file(self, admin_user):
        client = auth_client(admin_user)
        response = client.post(
            "/api/v1/finance/documents/",
            {
                "kind": "accounting_export",
                "payload": {"counterpart": "ОсОО Ромашка", "rows": [{"date": "01.09.2026", "debit": 100}]},
            },
            format="json",
        )
        assert response.status_code == 200
        assert "spreadsheetml" in response["Content-Type"]
        assert response.content[:2] == b"PK", "XLSX — это zip-контейнер"

    def test_reconciliation_send_queues_and_reports_channel(self, admin_user):
        client = auth_client(admin_user)
        body = client.post(
            "/api/v1/finance/documents/",
            {
                "kind": "reconciliation_send",
                "payload": {"counterpart": "ОсОО Ромашка", "email": "buh@example.com"},
            },
            format="json",
        ).json()
        assert body["status"] == "queued"
        assert body["recipient"] == "buh@example.com"
        assert body["channel_configured"] is True
        assert OutboundDelivery.all_objects.filter(pk=body["delivery"]).exists()


class TestSlaBreaches:
    def test_breach_is_recorded_and_published(self, tenant, operator_user):
        from common.models import OutboxEvent
        from workforce.models import SlaInstance, SlaPolicy
        from workforce.scheduled_tasks import check_sla_breaches

        policy = SlaPolicy.objects.create(
            tenant=tenant, event_type="order.created", response_minutes=15
        )
        started = timezone.now() - timezone.timedelta(minutes=60)
        instance = SlaInstance.objects.create(
            tenant=tenant,
            policy=policy,
            resource_type="Order",
            resource_id="00000000-0000-0000-0000-000000000001",
            assignee=operator_user,
            started_at=started,
            response_deadline=started + timezone.timedelta(minutes=15),
        )

        check_sla_breaches()

        instance.refresh_from_db()
        assert instance.breached_at is not None
        assert OutboxEvent.objects.filter(event_type="sla.breached", tenant=tenant).exists()

    def test_breach_is_recorded_once(self, tenant, operator_user):
        from common.models import OutboxEvent
        from workforce.models import SlaInstance, SlaPolicy
        from workforce.scheduled_tasks import check_sla_breaches

        policy = SlaPolicy.objects.create(tenant=tenant, event_type="order.created", response_minutes=5)
        started = timezone.now() - timezone.timedelta(hours=2)
        SlaInstance.objects.create(
            tenant=tenant,
            policy=policy,
            resource_type="Order",
            resource_id="00000000-0000-0000-0000-000000000002",
            assignee=operator_user,
            started_at=started,
            response_deadline=started + timezone.timedelta(minutes=5),
        )

        check_sla_breaches()
        check_sla_breaches()

        assert OutboxEvent.objects.filter(event_type="sla.breached", tenant=tenant).count() == 1


class TestAvatarVisibility:
    def test_operator_can_see_a_colleague_avatar(self, tenant, admin_user, operator_user):
        """Фото коллеги нужно в чатах и на дашборде, где users.manage нет."""
        response = auth_client(operator_user).get(f"/api/v1/users/{admin_user.id}/avatar/")
        # Аватар не загружен — но это 404, а не 403: право проверяться больше не должно.
        assert response.status_code == 404


class TestReports:
    def test_summary_groups_by_kind(self, tenant, admin_user):
        from crm.models import Company
        from orders.models import Order
        from services.models import OrderService

        company = Company.objects.create(tenant=tenant, legal_name="ОсОО Ромашка", short_name="Ромашка")
        order = Order.objects.create(
            tenant=tenant, number="ORD-1", client_company=company, operator=admin_user
        )
        OrderService.objects.create(
            tenant=tenant,
            order=order,
            kind="avia",
            title="LED-IST",
            status="issued",
            currency="USD",
            supplier_cost=Decimal("300.00"),
            client_total=Decimal("500.00"),
        )

        body = auth_client(admin_user).get("/api/v1/reports/summary/?group_by=kind").json()
        assert body["group_by"] == "kind"
        row = body["rows"][0]
        assert row["group"] == "Авиа"
        assert row["revenue"] == "500.00"
        assert row["profit"] == "200.00"
        assert body["totals"][0]["profit"] == "200.00"

    def test_xlsx_export(self, tenant, admin_user):
        response = auth_client(admin_user).get("/api/v1/reports/summary/?group_by=operator&format=xlsx")
        assert response.status_code == 200
        assert response.content[:2] == b"PK"
