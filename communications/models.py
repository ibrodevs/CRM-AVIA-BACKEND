from django.db import models

from common.models import TenantModel


class ChatThread(TenantModel):
    class Kind(models.TextChoices):
        INTERNAL = "internal"
        CLIENT = "client"
        SUPPLIER = "supplier"

    type = models.CharField(max_length=10, choices=Kind.choices)
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_threads"
    )
    service = models.ForeignKey(
        "services.OrderService", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_threads"
    )
    title = models.CharField(max_length=255, blank=True)
    external_channel = models.CharField(max_length=16, blank=True)
    external_account = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, default="active")

    class Meta:
        db_table = "communications_thread"
        indexes = [models.Index(fields=["tenant", "type", "status"])]


class ThreadParticipant(TenantModel):
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_participations"
    )
    person = models.ForeignKey(
        "crm.Person", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_participations"
    )
    external_identity = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=16, default="member")
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    last_read_message = models.ForeignKey(
        "communications.Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "communications_participant"
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "user"],
                condition=models.Q(user__isnull=False, left_at__isnull=True),
                name="uniq_thread_user",
            ),
        ]


class Message(TenantModel):
    class Kind(models.TextChoices):
        TEXT = "text"
        FILE = "file"
        SYSTEM = "system"
        SERVICE_CARD = "service_card"

    class DeliveryState(models.TextChoices):
        QUEUED = "queued"
        SENT = "sent"
        DELIVERED = "delivered"
        READ = "read"
        FAILED = "failed"

    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    author_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    author_external = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=14, choices=Kind.choices, default=Kind.TEXT)
    body = models.TextField(blank=True)
    reply_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies"
    )
    attachment = models.ForeignKey(
        "documents.DocumentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    service_card = models.ForeignKey(
        "offers.ServiceCard", null=True, blank=True, on_delete=models.PROTECT, related_name="messages"
    )
    external_message_id = models.CharField(max_length=128, blank=True)
    delivery_state = models.CharField(
        max_length=10, choices=DeliveryState.choices, default=DeliveryState.SENT
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    original_body = models.TextField(blank=True)

    class Meta:
        db_table = "communications_message"
        indexes = [
            models.Index(fields=["thread", "-id"]),
        ]


class WebhookEvent(models.Model):
    """Дедупликация входящих webhook: unique provider+external_event_id (ТЗ §30)."""

    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=32)
    external_event_id = models.CharField(max_length=128)
    payload = models.JSONField()
    signature_valid = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "communications_webhook_event"
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_event_id"], name="uniq_webhook_event"),
        ]


class OutboundMessageDelivery(models.Model):
    """Outbox-доставка исходящего сообщения во внешний канал (ТЗ §17)."""

    id = models.BigAutoField(primary_key=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=16)
    recipient = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=10, default="queued")
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "communications_outbound_delivery"
