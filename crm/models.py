"""CRM: лица, клиенты, компании, договоры, сборы (ТЗ §6)."""
from django.db import models

from common.fields import EncryptedTextField
from common.models import TenantModel


class Person(TenantModel):
    """Нормализованное физическое лицо. Один Person может быть клиентом,
    пассажиром, сотрудником компании и контактом одновременно."""

    class Gender(models.TextChoices):
        MALE = "male"
        FEMALE = "female"

    surname = models.CharField(max_length=100)
    given_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    latin_surname = models.CharField(max_length=100, blank=True)
    latin_given_name = models.CharField(max_length=100, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=6, choices=Gender.choices, blank=True)
    citizenship = models.CharField(max_length=2, blank=True)  # ISO 3166-1 alpha-2
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    preferred_language = models.CharField(max_length=8, blank=True)
    preferred_channel = models.CharField(max_length=32, blank=True)  # phone/telegram/whatsapp/email
    status = models.CharField(max_length=16, default="active")
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "crm_person"
        indexes = [
            models.Index(fields=["tenant", "surname", "given_name"]),
            models.Index(fields=["tenant", "phone"]),
            models.Index(fields=["tenant", "email"]),
            models.Index(fields=["tenant", "birth_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.surname} {self.given_name}".strip()

    @property
    def full_name(self) -> str:
        return " ".join(p for p in [self.surname, self.given_name, self.middle_name] if p)


class PersonDocument(TenantModel):
    """Документ лица. Номер шифруется; в обычных ответах маскируется (ТЗ §6.1).

    number_norm — нормализованный номер (без пробелов, верхний регистр) для
    контроля уникальности type+country+number в tenant (ТЗ §30) без
    расшифровки; хранится как SHA-256 в целях неразглашения.
    """

    class Kind(models.TextChoices):
        FOREIGN_PASSPORT = "foreign_passport"
        NATIONAL_PASSPORT = "national_passport"
        ID_CARD = "id_card"
        BIRTH_CERTIFICATE = "birth_certificate"
        VISA = "visa"
        OTHER = "other"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="documents")
    type = models.CharField(max_length=20, choices=Kind.choices)
    number = EncryptedTextField()
    number_hash = models.CharField(max_length=64, editable=False)
    series = models.CharField(max_length=32, blank=True)
    issued_at = models.DateField(null=True, blank=True)
    expires_at = models.DateField(null=True, blank=True)
    issuing_country = models.CharField(max_length=2, blank=True)
    issuing_authority = models.CharField(max_length=255, blank=True)
    nationality = models.CharField(max_length=2, blank=True)
    file = models.ForeignKey("documents.Document", null=True, blank=True,
                             on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=16, default="active")  # active/expired/replaced

    class Meta:
        db_table = "crm_person_document"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "type", "issuing_country", "number_hash"],
                condition=models.Q(archived_at__isnull=True),
                name="uniq_person_document_number",
            ),
        ]

    def save(self, *args, **kwargs):
        import hashlib

        if self.number:
            normalized = str(self.number).replace(" ", "").upper()
            self.number_hash = hashlib.sha256(normalized.encode()).hexdigest()
        super().save(*args, **kwargs)


class LoyaltyCard(TenantModel):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="loyalty_cards")
    program_type = models.CharField(max_length=32)  # airline/hotel/rail/other
    provider = models.CharField(max_length=100)
    number = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default="active")
    auto_apply = models.BooleanField(default=False)
    valid_until = models.DateField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "crm_loyalty_card"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "provider", "number"],
                                    name="uniq_loyalty_card"),
        ]


class ClientProfile(TenantModel):
    """Профиль лица как клиента. Агрегаты (orders_count, revenue, debt)
    рассчитываются read models, не хранятся как истина от клиента."""

    class Status(models.TextChoices):
        NEW = "new"
        ACTIVE = "active"
        VIP = "vip"
        INACTIVE = "inactive"

    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name="client_profile")
    client_type = models.CharField(max_length=16, default="individual")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    source = models.CharField(max_length=64, blank=True)
    assigned_manager = models.ForeignKey("accounts.User", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "crm_client_profile"
        indexes = [models.Index(fields=["tenant", "status"])]


class Company(TenantModel):
    class Status(models.TextChoices):
        ACTIVE = "active"
        PAUSED = "paused"
        ARCHIVED = "archived"

    legal_name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=150, blank=True)
    type = models.CharField(max_length=32, blank=True)  # llc/jsc/ie/...
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    tax_id = models.CharField(max_length=32, blank=True)  # ИНН
    okpo = models.CharField(max_length=32, blank=True)
    vat_mode = models.CharField(max_length=32, blank=True)
    legal_address = models.TextField(blank=True)
    bank_name = models.CharField(max_length=255, blank=True)
    bank_account = EncryptedTextField(blank=True)  # счёт/IBAN — шифруется
    director = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    requires_e_sign = models.BooleanField(default=False)
    assigned_manager = models.ForeignKey("accounts.User", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "crm_company"
        constraints = [
            # partial unique для неархивных компаний (ТЗ §30)
            models.UniqueConstraint(
                fields=["tenant", "tax_id"],
                condition=models.Q(archived_at__isnull=True) & ~models.Q(tax_id=""),
                name="uniq_company_tax_id",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self) -> str:
        return self.short_name or self.legal_name


class CompanyContact(TenantModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="contacts")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="company_contacts")
    role = models.CharField(max_length=100, blank=True)  # директор, координатор, ...
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "crm_company_contact"
        constraints = [
            models.UniqueConstraint(fields=["company", "person"], name="uniq_company_contact"),
        ]


class Department(TenantModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="departments")
    name = models.CharField(max_length=150)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="children")
    travel_policy = models.ForeignKey("travel_policy.TravelPolicy", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "crm_department"


class Employee(TenantModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="employees")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="employments")
    personnel_number = models.CharField(max_length=32, blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="employees")
    position = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=16, default="active")

    class Meta:
        db_table = "crm_employee"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "person"],
                condition=models.Q(archived_at__isnull=True),
                name="uniq_company_employee",
            ),
        ]


class Contract(TenantModel):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="contracts")
    number = models.CharField(max_length=64)
    signed_at = models.DateField(null=True, blank=True)
    starts_at = models.DateField(null=True, blank=True)
    ends_at = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, default="draft")  # draft/active/expired/terminated
    file = models.ForeignKey("documents.Document", null=True, blank=True,
                             on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "crm_contract"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "company", "number"],
                                    name="uniq_contract_number"),
        ]


class Agreement(TenantModel):
    """Версия условий договора. Активная версия на дату заказа выбирается
    сервером; в заказе сохраняется snapshot (ТЗ §6.2)."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="agreements")
    number = models.CharField(max_length=64)
    agreement_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, default="draft")  # draft/active/superseded/expired
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    fee_template = models.ForeignKey("crm.FeeTemplate", null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="+")
    service_descriptions = models.JSONField(default=list, blank=True)
    fee_descriptions = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "crm_agreement"
        constraints = [
            models.UniqueConstraint(fields=["contract", "agreement_version"],
                                    name="uniq_agreement_version"),
        ]


class SettlementProfile(TenantModel):
    """Расчётный профиль компании: prepayment/deposit/credit (ТЗ §6.2)."""

    class Mode(models.TextChoices):
        PREPAYMENT = "prepayment"
        DEPOSIT = "deposit"
        CREDIT = "credit"

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="settlement")
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.PREPAYMENT)
    currency = models.CharField(max_length=3, default="USD")
    deposit_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deposit_reserved = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_days = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "crm_settlement_profile"
        constraints = [
            # депозитный резерв не превышает баланс, кроме audited override (ТЗ §30)
            models.CheckConstraint(
                condition=models.Q(deposit_reserved__lte=models.F("deposit_balance"))
                | models.Q(mode__in=["prepayment", "credit"]),
                name="check_deposit_reserve",
            ),
        ]


class FeeTemplate(TenantModel):
    """Шаблон набора сборов, привязываемый к Agreement."""

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "crm_fee_template"


class FeeRule(TenantModel):
    """Правило сбора (ТЗ §6.3). Расчёт — серверный, ROUND_HALF_UP."""

    class FeeKind(models.TextChoices):
        SERVICE = "service"
        MARKUP = "markup"
        ISSUE = "issue"
        EXCHANGE = "exchange"
        REFUND = "refund"
        SUPPLIER = "supplier"

    class Calculation(models.TextChoices):
        FIXED = "fixed"
        PERCENT = "percent"

    template = models.ForeignKey(FeeTemplate, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="rules")
    agreement = models.ForeignKey(Agreement, null=True, blank=True, on_delete=models.CASCADE,
                                  related_name="fee_rules")
    service_kind = models.CharField(max_length=32)  # avia/rail/hotel/... или "*"
    fee_kind = models.CharField(max_length=10, choices=FeeKind.choices)
    calculation = models.CharField(max_length=8, choices=Calculation.choices)
    value = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, blank=True)  # обязательна для fixed
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "crm_fee_rule"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(calculation="fixed") | ~models.Q(currency=""),
                name="check_fixed_fee_has_currency",
            ),
        ]
