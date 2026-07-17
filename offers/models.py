import secrets

from django.db import models

from common.models import TenantModel


class ProposalNumberCounter(models.Model):
    tenant = models.OneToOneField(
        "tenancy.Organization", primary_key=True, on_delete=models.CASCADE, related_name="+"
    )
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "offers_proposal_counter"

    @classmethod
    def next_number(cls, tenant_id) -> str:
        counter, _ = cls.objects.get_or_create(tenant_id=tenant_id)
        counter = cls.objects.select_for_update().get(pk=counter.pk)
        counter.last_value += 1
        counter.save(update_fields=["last_value"])
        return f"KP-{counter.last_value:06d}"


class ProposalTemplate(TenantModel):
    code = models.SlugField(max_length=63)
    name = models.CharField(max_length=150)
    body = models.TextField(blank=True)
    template_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, default="draft")

    class Meta:
        db_table = "offers_proposal_template"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code", "template_version"], name="uniq_proposal_template_version"
            ),
        ]


class Proposal(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PREPARED = "prepared"
        SENT = "sent"
        UNDER_REVIEW = "under_review"
        APPROVED = "approved"
        REJECTED = "rejected"
        ARCHIVED = "archived"

    number = models.CharField(max_length=20)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="proposals")
    type = models.CharField(max_length=32, blank=True)
    purpose = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.DRAFT)
    currency = models.CharField(max_length=3, default="USD")
    valid_until = models.DateTimeField(null=True, blank=True)
    template = models.ForeignKey(
        ProposalTemplate, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    current_version = models.PositiveIntegerField(default=0)
    approved_variant = models.ForeignKey(
        "offers.ProposalVariant", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "offers_proposal"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_proposal_number"),
        ]
        indexes = [models.Index(fields=["tenant", "order", "status"])]


PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    Proposal.Status.DRAFT: {Proposal.Status.PREPARED, Proposal.Status.ARCHIVED},
    Proposal.Status.PREPARED: {Proposal.Status.SENT, Proposal.Status.DRAFT, Proposal.Status.ARCHIVED},
    Proposal.Status.SENT: {
        Proposal.Status.UNDER_REVIEW,
        Proposal.Status.APPROVED,
        Proposal.Status.REJECTED,
        Proposal.Status.ARCHIVED,
    },
    Proposal.Status.UNDER_REVIEW: {
        Proposal.Status.APPROVED,
        Proposal.Status.REJECTED,
        Proposal.Status.ARCHIVED,
    },
    Proposal.Status.APPROVED: {Proposal.Status.ARCHIVED},
    Proposal.Status.REJECTED: {Proposal.Status.DRAFT, Proposal.Status.ARCHIVED},
    Proposal.Status.ARCHIVED: set(),
}


class ProposalVariant(TenantModel):
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=150)
    sequence = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=12, default="proposed")
    comment = models.TextField(blank=True)

    class Meta:
        db_table = "offers_proposal_variant"
        constraints = [
            models.UniqueConstraint(fields=["proposal", "sequence"], name="uniq_variant_sequence"),
            models.UniqueConstraint(
                fields=["proposal"], condition=models.Q(status="approved"), name="uniq_approved_variant"
            ),
        ]


class ProposalItem(TenantModel):
    variant = models.ForeignKey(ProposalVariant, on_delete=models.CASCADE, related_name="items")
    offer = models.ForeignKey(
        "services.ServiceOffer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proposal_items",
    )
    service = models.ForeignKey(
        "services.OrderService",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proposal_items",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    quantity = models.PositiveSmallIntegerField(default=1)
    price_amount = models.DecimalField(max_digits=14, decimal_places=2)
    price_currency = models.CharField(max_length=3)
    price_snapshot = models.ForeignKey(
        "services.PriceSnapshot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "offers_proposal_item"


class ProposalVersion(models.Model):
    """Immutable snapshot документа и цен (ТЗ §12.1). Sent-версия неизменна."""

    id = models.BigAutoField(primary_key=True)
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    template_version = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "offers_proposal_version"
        constraints = [
            models.UniqueConstraint(fields=["proposal", "version"], name="uniq_proposal_version"),
        ]


def _card_token() -> str:
    return secrets.token_urlsafe(32)


class ServiceCard(TenantModel):
    """Карточка услуги для клиента (ТЗ §12.2)."""

    class Status(models.TextChoices):
        CREATED = "created"
        SENT = "sent"
        DELIVERED = "delivered"
        VIEWED = "viewed"
        CHOSEN = "chosen"
        DECLINED = "declined"
        EXPIRED = "expired"
        PRICE_CHANGED = "price_changed"
        UNAVAILABLE = "unavailable"
        ISSUED = "issued"

    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.PROTECT, related_name="service_cards"
    )
    service = models.ForeignKey(
        "services.OrderService", null=True, blank=True, on_delete=models.PROTECT, related_name="service_cards"
    )
    offer = models.ForeignKey(
        "services.ServiceOffer", null=True, blank=True, on_delete=models.PROTECT, related_name="service_cards"
    )
    kind = models.CharField(max_length=16)
    scenario = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.CREATED)
    valid_until = models.DateTimeField(null=True, blank=True)
    price_snapshot = models.JSONField(null=True, blank=True)
    content = models.JSONField(default=dict, blank=True)
    card_version = models.PositiveIntegerField(default=1)
    public_token = models.CharField(max_length=64, unique=True, default=_card_token)

    class Meta:
        db_table = "offers_service_card"
        indexes = [models.Index(fields=["tenant", "order", "status"])]


class ServiceCardDelivery(TenantModel):
    """Channel-specific доставка карточки (internal/telegram/whatsapp/max/email)."""

    class State(models.TextChoices):
        QUEUED = "queued"
        SENT = "sent"
        DELIVERED = "delivered"
        READ = "read"
        FAILED = "failed"

    card = models.ForeignKey(ServiceCard, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=16)
    recipient = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED)
    external_message_id = models.CharField(max_length=128, blank=True)
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "offers_card_delivery"


class ServiceCardResponse(models.Model):
    """Реакция клиента: один terminal choice на card version; повтор возвращает
    прежний результат (ТЗ §30)."""

    id = models.BigAutoField(primary_key=True)
    card = models.ForeignKey(ServiceCard, on_delete=models.CASCADE, related_name="responses")
    card_version = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    comment = models.TextField(blank=True)
    channel = models.CharField(max_length=16, blank=True)
    external_identity = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "offers_card_response"
        constraints = [
            models.UniqueConstraint(
                fields=["card", "card_version"],
                condition=models.Q(action__in=["choose", "decline"]),
                name="uniq_terminal_card_response",
            ),
        ]
