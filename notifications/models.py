"""Уведомления (ТЗ §18)."""
from django.db import models

from common.models import TenantModel


class NotificationRule(TenantModel):
    """Сопоставление события получателям, приоритету и каналам (admin)."""

    event_type = models.CharField(max_length=100)  # шаблон, напр. "order.*"
    name = models.CharField(max_length=150)
    priority = models.CharField(max_length=8, default="medium")  # critical/high/medium/info
    recipients = models.JSONField(default=dict, blank=True)
    # {"roles": ["operator"], "users": [...], "responsible": true}
    channels = models.JSONField(default=list, blank=True)  # ["desktop","email","telegram"]
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "notifications_rule"


class Notification(models.Model):
    """Персональная запись уведомления. Состояния read/pinned/dismissed
    не влияют на других пользователей (ТЗ §18)."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.CASCADE,
                               related_name="+")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE,
                             related_name="notifications")
    priority = models.CharField(max_length=8, default="medium")
    source = models.CharField(max_length=32, blank=True)  # orders/finance/chats/...
    event_type = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    deep_link = models.CharField(max_length=255, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_notification"
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user"], name="idx_notification_unread",
                         condition=models.Q(read_at__isnull=True,
                                            dismissed_at__isnull=True)),
        ]


class NotificationDelivery(models.Model):
    """Доставка во внешний канал (desktop/email/telegram/whatsapp/max/push)."""

    id = models.BigAutoField(primary_key=True)
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE,
                                     related_name="deliveries")
    channel = models.CharField(max_length=16)
    state = models.CharField(max_length=10, default="queued")  # queued/sent/failed
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "notifications_delivery"


class DeadlineThreshold(models.Model):
    """Уникальный ключ (rule, resource, threshold, recipient) — периодическая
    команда не создаёт дубли (ТЗ §18, §30)."""

    id = models.BigAutoField(primary_key=True)
    rule_key = models.CharField(max_length=100)   # напр. "ticketing_deadline_2h"
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=64)
    threshold = models.CharField(max_length=32)
    recipient = models.ForeignKey("accounts.User", on_delete=models.CASCADE,
                                  related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_deadline_threshold"
        constraints = [
            models.UniqueConstraint(
                fields=["rule_key", "resource_type", "resource_id", "threshold",
                        "recipient"],
                name="uniq_deadline_threshold",
            ),
        ]
