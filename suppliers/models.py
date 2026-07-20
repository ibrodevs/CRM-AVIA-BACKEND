from django.db import models

from common.fields import EncryptedTextField
from common.models import TenantModel


class Supplier(TenantModel):
    class Status(models.TextChoices):
        ACTIVE = "active"
        PAUSED = "paused"
        ARCHIVED = "archived"

    name = models.CharField(max_length=255)
    legal_name = models.CharField(max_length=255, blank=True)
    tax_id = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    organization_type = models.CharField(max_length=32, blank=True)
    is_global = models.BooleanField(default=False)
    service_kinds = models.JSONField(default=list, blank=True)
    countries = models.JSONField(default=list, blank=True)
    cities = models.JSONField(default=list, blank=True)
    currencies = models.JSONField(default=list, blank=True)
    communication_methods = models.JSONField(default=list, blank=True)
    work_hours = models.CharField(max_length=100, blank=True)
    settlement_type = models.CharField(max_length=32, blank=True)
    contract_number = models.CharField(max_length=64, blank=True)
    contact_person = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    automation_capabilities = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "suppliers_supplier"
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "tax_id"])]

    def __str__(self) -> str:
        return self.name


class SupplierCredential(TenantModel):
    """API-доступ поставщика. Секрет шифруется, наружу не возвращается (ТЗ §5.3, §13)."""

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="credentials")
    provider_adapter = models.CharField(max_length=100)
    environment = models.CharField(max_length=16, default="sandbox")
    encrypted_secrets = EncryptedTextField(blank=True)
    status = models.CharField(max_length=16, default="inactive")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    rotated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "suppliers_credential"
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "provider_adapter", "environment"],
                condition=models.Q(archived_at__isnull=True),
                name="uniq_supplier_credential",
            ),
        ]


class SupplierMarkupRule(TenantModel):
    """Правило наценки поставщика. Resolver возвращает применённое правило
    и объяснение (ТЗ §13)."""

    class AmountType(models.TextChoices):
        FIXED = "fixed"
        PERCENT = "percent"

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="markup_rules")
    service_kind = models.CharField(max_length=32, default="*")
    route = models.CharField(max_length=100, blank=True)
    geography = models.CharField(max_length=100, blank=True)
    airline = models.CharField(max_length=8, blank=True)
    cabin = models.CharField(max_length=32, blank=True)
    passenger_category = models.CharField(max_length=32, blank=True)
    amount_type = models.CharField(max_length=8, choices=AmountType.choices)
    amount_value = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, blank=True)
    priority = models.IntegerField(default=100)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "suppliers_markup_rule"
        indexes = [models.Index(fields=["tenant", "supplier", "service_kind", "priority"])]


class SupplierSearchPriority(TenantModel):
    """Порядок опроса поставщиков по типу услуги с fallback (ТЗ §13)."""

    service_kind = models.CharField(max_length=32)
    ordered_suppliers = models.JSONField(default=list)
    conditions = models.JSONField(default=dict, blank=True)
    fallback_supplier = models.ForeignKey(
        Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "suppliers_search_priority"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "service_kind"],
                condition=models.Q(is_active=True) & models.Q(archived_at__isnull=True),
                name="uniq_active_search_priority",
            ),
        ]
