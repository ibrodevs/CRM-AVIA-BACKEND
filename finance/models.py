"""Финансы: двойной ledger, обязательства, платежи, возвраты (ТЗ §14).

Принципы: только Decimal; записи не удаляются и не переписываются — исправление
через reversal; баланс/долг считаются из проводок.
"""
from django.db import models

from common.models import TenantModel


class FinancialAccount(TenantModel):
    class Kind(models.TextChoices):
        CASH = "cash"
        BANK = "bank"
        CLIENT_RECEIVABLE = "client_receivable"
        SUPPLIER_PAYABLE = "supplier_payable"
        DEPOSIT = "deposit"
        REVENUE = "revenue"
        EXPENSE = "expense"
        FEE_REVENUE = "fee_revenue"

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    currency = models.CharField(max_length=3)
    company = models.ForeignKey("crm.Company", null=True, blank=True,
                                on_delete=models.PROTECT, related_name="finance_accounts")
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="finance_accounts")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "finance_account"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code", "currency"],
                                    name="uniq_account_code"),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.currency})"


class LedgerTransaction(models.Model):
    """Сбалансированная группа проводок: сумма дебета = сумме кредита (ТЗ §30)."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.PROTECT,
                               related_name="+")
    occurred_at = models.DateTimeField()
    description = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=32)  # payment/refund/accrual/reversal/adjustment
    order = models.ForeignKey("orders.Order", null=True, blank=True,
                              on_delete=models.PROTECT, related_name="ledger_transactions")
    payment = models.ForeignKey("finance.Payment", null=True, blank=True,
                                on_delete=models.PROTECT, related_name="ledger_transactions")
    reverses = models.OneToOneField("self", null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="reversed_by")
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_ledger_transaction"
        indexes = [models.Index(fields=["tenant", "-occurred_at"])]


class LedgerEntry(models.Model):
    class Direction(models.TextChoices):
        DEBIT = "debit"
        CREDIT = "credit"

    id = models.BigAutoField(primary_key=True)
    transaction = models.ForeignKey(LedgerTransaction, on_delete=models.CASCADE,
                                    related_name="entries")
    account = models.ForeignKey(FinancialAccount, on_delete=models.PROTECT,
                                related_name="entries")
    direction = models.CharField(max_length=6, choices=Direction.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)

    class Meta:
        db_table = "finance_ledger_entry"
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0),
                                   name="check_entry_positive"),
        ]
        indexes = [models.Index(fields=["account"])]


class FinancialObligation(TenantModel):
    class Direction(models.TextChoices):
        CLIENT_RECEIVABLE = "client_receivable"
        SUPPLIER_PAYABLE = "supplier_payable"
        CLIENT_REFUND = "client_refund"
        SUPPLIER_REFUND = "supplier_refund"

    class Status(models.TextChoices):
        OPEN = "open"
        PARTIAL = "partial"
        SETTLED = "settled"
        CANCELLED = "cancelled"

    order = models.ForeignKey("orders.Order", null=True, blank=True,
                              on_delete=models.PROTECT, related_name="obligations")
    service = models.ForeignKey("services.OrderService", null=True, blank=True,
                                on_delete=models.PROTECT, related_name="obligations")
    aftersale_case = models.ForeignKey("aftersales.AfterSaleCase", null=True, blank=True,
                                       on_delete=models.PROTECT, related_name="obligations")
    direction = models.CharField(max_length=20, choices=Direction.choices)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3)
    original_amount = models.DecimalField(max_digits=14, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    class Meta:
        db_table = "finance_obligation"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(paid_amount__lte=models.F("original_amount")),
                name="check_paid_lte_original",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "order", "direction"]),
            models.Index(fields=["tenant", "status", "due_date"]),
        ]

    @property
    def outstanding_amount(self):
        return self.original_amount - self.paid_amount


class Payment(TenantModel):
    class Direction(models.TextChoices):
        INCOMING = "incoming"
        OUTGOING = "outgoing"

    class Status(models.TextChoices):
        DRAFT = "draft"
        PENDING = "pending"
        CONFIRMED = "confirmed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    direction = models.CharField(max_length=8, choices=Direction.choices,
                                 default=Direction.INCOMING)
    order = models.ForeignKey("orders.Order", null=True, blank=True,
                              on_delete=models.PROTECT, related_name="payments")
    payer_person = models.ForeignKey("crm.Person", null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="payments")
    payer_company = models.ForeignKey("crm.Company", null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="payments")
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=32, blank=True)  # cash/bank_transfer/card/deposit
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    exchange_rate_snapshot = models.JSONField(null=True, blank=True)
    provider_transaction_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    comment = models.TextField(blank=True)

    class Meta:
        db_table = "finance_payment"
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0),
                                   name="check_payment_positive"),
            models.UniqueConstraint(
                fields=["tenant", "provider_transaction_id"],
                condition=~models.Q(provider_transaction_id=""),
                name="uniq_provider_transaction",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status", "-created_at"])]


class PaymentAllocation(TenantModel):
    """Распределение платежа: сумма allocations <= подтверждённый платёж (ТЗ §30)."""

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT,
                                related_name="allocations")
    obligation = models.ForeignKey(FinancialObligation, on_delete=models.PROTECT,
                                   related_name="allocations")
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = "finance_payment_allocation"
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0),
                                   name="check_allocation_positive"),
        ]


class Refund(TenantModel):
    """Возврат: refund = paid - penalty - fee - withholdings, >= 0 (ТЗ §14.4)."""

    class Status(models.TextChoices):
        DRAFT = "draft"
        APPROVED = "approved"
        EXECUTED = "executed"
        CANCELLED = "cancelled"

    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="refunds")
    obligation = models.ForeignKey(FinancialObligation, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="refunds")
    aftersale_case = models.ForeignKey("aftersales.AfterSaleCase", null=True, blank=True,
                                       on_delete=models.PROTECT, related_name="refunds")
    currency = models.CharField(max_length=3)
    original_paid = models.DecimalField(max_digits=14, decimal_places=2)
    supplier_penalty = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    agency_service_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_withholdings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2)
    formula_snapshot = models.JSONField()  # компоненты и формула
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    executed_at = models.DateTimeField(null=True, blank=True)
    executed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "finance_refund"
        constraints = [
            models.CheckConstraint(condition=models.Q(refund_amount__gte=0),
                                   name="check_refund_non_negative"),
        ]


class ExchangeRate(TenantModel):
    """Курс: версионируется по источнику и времени (ТЗ §14.1)."""

    source = models.CharField(max_length=32, default="manual")  # manual/nbkr/api
    from_currency = models.CharField(max_length=3)
    to_currency = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    as_of = models.DateTimeField()

    class Meta:
        db_table = "finance_exchange_rate"
        indexes = [models.Index(fields=["tenant", "from_currency", "to_currency",
                                        "-as_of"])]


class Invoice(TenantModel):
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT,
                              related_name="invoices")
    number = models.CharField(max_length=32)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, default="issued")  # issued/paid/void
    document = models.ForeignKey("documents.Document", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "finance_invoice"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_invoice_number"),
        ]


class ReconciliationImport(TenantModel):
    """Импорт банковской выписки для сверки (ТЗ §14.4)."""

    file_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, default="parsed")  # parsed/matched/completed
    rows_total = models.PositiveIntegerField(default=0)
    rows_matched = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "finance_reconciliation_import"


class ReconciliationRow(models.Model):
    id = models.BigAutoField(primary_key=True)
    import_job = models.ForeignKey(ReconciliationImport, on_delete=models.CASCADE,
                                   related_name="rows")
    row_index = models.PositiveIntegerField()
    date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    reference = models.CharField(max_length=255, blank=True)
    counterparty = models.CharField(max_length=255, blank=True)
    matched_payment = models.ForeignKey(Payment, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    match_type = models.CharField(max_length=10, blank=True)  # auto/manual
    status = models.CharField(max_length=12, default="unmatched")  # unmatched/matched/ignored

    class Meta:
        db_table = "finance_reconciliation_row"


class SalaryAccrual(TenantModel):
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT,
                             related_name="salary_accruals")
    period = models.CharField(max_length=7)  # YYYY-MM
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "finance_salary_accrual"
