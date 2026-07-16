"""Унифицированная модель услуг, поиск и ценообразование (ТЗ §8)."""
from django.db import models

from common.models import TenantModel

SERVICE_KINDS = [
    "avia", "rail", "hotel", "transfer", "bus", "tour",
    "aeroexpress", "lounge", "insurance", "visa", "other",
]
SERVICE_KIND_CHOICES = [(k, k) for k in SERVICE_KINDS]


class OrderService(TenantModel):
    """Услуга заказа — общий lifecycle для всех kind (ТЗ §8.1)."""

    class Status(models.TextChoices):
        SEARCHING = "searching"
        PROPOSED = "proposed"
        APPROVAL = "approval"
        BOOKED = "booked"
        CONFIRMED = "confirmed"
        ISSUED = "issued"
        REFUND_IN_PROGRESS = "refund_in_progress"
        REFUNDED = "refunded"
        CANCELLED = "cancelled"
        FAILED = "failed"

    class Source(models.TextChoices):
        API = "api"
        MANUAL = "manual"
        IMPORT = "import"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="services")
    kind = models.CharField(max_length=16, choices=SERVICE_KIND_CHOICES)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    title = models.CharField(max_length=255)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="services")
    external_id = models.CharField(max_length=128, blank=True)  # PNR/locator/booking ref
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.API)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    currency = models.CharField(max_length=3, default="USD")
    supplier_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    taxes = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    agency_fee = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    markup = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    commission = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    client_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    payment_deadline = models.DateTimeField(null=True, blank=True)
    ticketing_deadline = models.DateTimeField(null=True, blank=True)
    responsible = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="responsible_services")
    provider_snapshot = models.JSONField(null=True, blank=True)  # raw provider payload
    policy_compliance = models.JSONField(null=True, blank=True)  # результат travel policy
    cancellation_rules = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "services_order_service"
        indexes = [
            models.Index(fields=["tenant", "order", "status"]),
            models.Index(fields=["tenant", "kind", "status"]),
            models.Index(fields=["tenant", "responsible"]),
            models.Index(fields=["tenant", "ticketing_deadline"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.title}"


# Переходы статусов услуги (уточняются по kind на уровне сервиса).
SERVICE_TRANSITIONS: dict[str, set[str]] = {
    OrderService.Status.SEARCHING: {OrderService.Status.PROPOSED, OrderService.Status.FAILED,
                                    OrderService.Status.CANCELLED},
    OrderService.Status.PROPOSED: {OrderService.Status.APPROVAL, OrderService.Status.BOOKED,
                                   OrderService.Status.CANCELLED, OrderService.Status.FAILED},
    OrderService.Status.APPROVAL: {OrderService.Status.PROPOSED, OrderService.Status.BOOKED,
                                   OrderService.Status.CANCELLED},
    OrderService.Status.BOOKED: {OrderService.Status.CONFIRMED, OrderService.Status.ISSUED,
                                 OrderService.Status.CANCELLED, OrderService.Status.FAILED},
    OrderService.Status.CONFIRMED: {OrderService.Status.ISSUED, OrderService.Status.CANCELLED,
                                    OrderService.Status.FAILED},
    OrderService.Status.ISSUED: {OrderService.Status.REFUND_IN_PROGRESS,
                                 OrderService.Status.CANCELLED},
    OrderService.Status.REFUND_IN_PROGRESS: {OrderService.Status.REFUNDED,
                                             OrderService.Status.ISSUED},
    OrderService.Status.REFUNDED: set(),
    OrderService.Status.CANCELLED: set(),
    OrderService.Status.FAILED: {OrderService.Status.SEARCHING, OrderService.Status.PROPOSED},
}


class ServicePassenger(TenantModel):
    """Связь услуга-участник: индивидуальный тариф/место/статус (ТЗ §8.1)."""

    service = models.ForeignKey(OrderService, on_delete=models.CASCADE, related_name="passengers")
    participant = models.ForeignKey("orders.OrderParticipant", on_delete=models.PROTECT,
                                    related_name="service_passengers")
    fare_code = models.CharField(max_length=64, blank=True)
    room_ref = models.CharField(max_length=64, blank=True)
    seat_ref = models.CharField(max_length=16, blank=True)
    document = models.ForeignKey("crm.PersonDocument", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="+")
    currency = models.CharField(max_length=3, blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, default="active")  # active/issued/refunded/cancelled/replaced

    class Meta:
        db_table = "services_service_passenger"
        constraints = [
            # unique service+participant; удаление после брони запрещено — только cancel/replace
            models.UniqueConstraint(fields=["service", "participant"],
                                    name="uniq_service_passenger"),
        ]


class ServiceExtraCatalogItem(TenantModel):
    """Каталог допуслуг (настраивается администратором, ТЗ §21.1)."""

    kind = models.CharField(max_length=16, choices=SERVICE_KIND_CHOICES, default="avia")
    code = models.SlugField(max_length=63)
    name = models.CharField(max_length=150)
    stage = models.CharField(max_length=16, default="before_booking")
    default_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "services_extra_catalog"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "kind", "code"], name="uniq_extra_code"),
        ]


class ServiceExtra(TenantModel):
    """Дополнительная услуга к OrderService (ТЗ §8.1)."""

    class Stage(models.TextChoices):
        BEFORE_BOOKING = "before_booking"
        AFTER_BOOKING = "after_booking"
        AFTER_ISSUE = "after_issue"

    class Availability(models.TextChoices):
        PROVIDER = "provider"
        MANUAL = "manual"
        UNAVAILABLE = "unavailable"

    service = models.ForeignKey(OrderService, on_delete=models.CASCADE, related_name="extras")
    catalog_item = models.ForeignKey(ServiceExtraCatalogItem, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=150)
    stage = models.CharField(max_length=16, choices=Stage.choices)
    availability = models.CharField(max_length=12, choices=Availability.choices,
                                    default=Availability.MANUAL)
    passenger = models.ForeignKey(ServicePassenger, null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="extras")
    quantity = models.PositiveSmallIntegerField(default=1)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=16, default="proposed")  # proposed/confirmed/issued/cancelled
    emd_reference = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "services_service_extra"


# --- Поиск (ТЗ §8.2) --------------------------------------------------------

class SearchSession(TenantModel):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        PARTIAL = "partial"      # часть поставщиков упала
        FAILED = "failed"
        CANCELLED = "cancelled"
        EXPIRED = "expired"

    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="searches")
    order = models.ForeignKey("orders.Order", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="searches")
    kind = models.CharField(max_length=16, choices=SERVICE_KIND_CHOICES)
    criteria = models.JSONField()  # нормализованные критерии
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    job = models.ForeignKey("common.BackgroundJob", null=True, blank=True,
                            on_delete=models.SET_NULL, related_name="+")
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "services_search_session"
        indexes = [models.Index(fields=["tenant", "user", "-created_at"])]


class SearchProviderRun(TenantModel):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        TIMEOUT = "timeout"
        SKIPPED = "skipped"

    session = models.ForeignKey(SearchSession, on_delete=models.CASCADE, related_name="provider_runs")
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    provider_adapter = models.CharField(max_length=100)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    log = models.ForeignKey("integrations.IntegrationLog", null=True, blank=True,
                            on_delete=models.SET_NULL, related_name="+")
    error_code = models.CharField(max_length=100, blank=True)
    offers_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "services_search_provider_run"


class ServiceOffer(TenantModel):
    """Нормализованный вариант из поиска или введённый вручную (ТЗ §1.2, §8.2)."""

    session = models.ForeignKey(SearchSession, null=True, blank=True,
                                on_delete=models.CASCADE, related_name="offers")
    kind = models.CharField(max_length=16, choices=SERVICE_KIND_CHOICES)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="offers")
    provider_adapter = models.CharField(max_length=100, blank=True)
    external_key = models.CharField(max_length=255, blank=True)
    is_manual = models.BooleanField(default=False)
    itinerary = models.JSONField()      # нормализованный маршрут/продукт
    fare = models.JSONField(null=True, blank=True)
    price_amount = models.DecimalField(max_digits=14, decimal_places=2)
    price_currency = models.CharField(max_length=3)
    availability = models.CharField(max_length=32, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    raw_snapshot = models.JSONField(null=True, blank=True)
    dedup_hash = models.CharField(max_length=64, blank=True)  # для дедупликации эквивалентов
    exchange_rate_snapshot = models.JSONField(null=True, blank=True)
    applied_markup_rules = models.JSONField(null=True, blank=True)
    compliance = models.JSONField(null=True, blank=True)  # travel policy результат

    class Meta:
        db_table = "services_service_offer"
        indexes = [
            models.Index(fields=["tenant", "session", "price_amount"]),
            models.Index(fields=["tenant", "dedup_hash"]),
        ]


class PriceSnapshot(models.Model):
    """Immutable снимок расчёта цены на коммерчески значимом шаге (ТЗ §8.3)."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.CASCADE, related_name="+")
    service = models.ForeignKey(OrderService, null=True, blank=True,
                                on_delete=models.CASCADE, related_name="price_snapshots")
    offer = models.ForeignKey(ServiceOffer, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="price_snapshots")
    step = models.CharField(max_length=32)  # search/attach/booking/issue/refund
    components = models.JSONField()  # {base, taxes, fees:[...], markup, discount, total}
    formula = models.TextField(blank=True)
    rate_source = models.CharField(max_length=64, blank=True)
    rate_timestamp = models.DateTimeField(null=True, blank=True)
    rounding = models.CharField(max_length=32, default="ROUND_HALF_UP")
    currency = models.CharField(max_length=3)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "services_price_snapshot"
