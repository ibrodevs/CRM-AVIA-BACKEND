import uuid

from django.db import models

from common.models import TenantModel


class Document(TenantModel):
    class Kind(models.TextChoices):
        ITINERARY_RECEIPT = "itinerary_receipt"
        TICKET = "ticket"
        VOUCHER = "voucher"
        INSURANCE_POLICY = "insurance_policy"
        INVOICE = "invoice"
        ACT = "act"
        CONTRACT = "contract"
        PASSPORT = "passport"
        APPLICATION = "application"
        SUPPLIER_CONFIRMATION = "supplier_confirmation"
        CERTIFICATE = "certificate"
        OTHER = "other"

    class Status(models.TextChoices):
        DRAFT = "draft"
        GENERATED = "generated"
        ACCOUNTING = "accounting"
        SIGNING = "signing"
        SIGNED = "signed"
        VOID = "void"

    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.PROTECT, related_name="documents"
    )
    service = models.ForeignKey(
        "services.OrderService", null=True, blank=True, on_delete=models.PROTECT, related_name="documents"
    )
    person = models.ForeignKey(
        "crm.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="files"
    )
    company = models.ForeignKey(
        "crm.Company", null=True, blank=True, on_delete=models.PROTECT, related_name="files"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    title = models.CharField(max_length=255)
    source = models.CharField(max_length=16, default="upload")
    current_version = models.PositiveIntegerField(default=0)
    document_date = models.DateField(null=True, blank=True)
    document_number = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    requires_signing = models.BooleanField(default=False)
    is_confidential = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "documents_document"
        indexes = [
            models.Index(fields=["tenant", "order", "kind"]),
            models.Index(fields=["tenant", "status"]),
        ]


def _version_upload_path(instance, filename) -> str:
    return f"documents/{instance.document.tenant_id}/{instance.document_id}/{uuid.uuid4().hex}/{filename}"


class DocumentVersion(models.Model):
    """Immutable версия файла документа (ТЗ §15.1, §30)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    file = models.FileField(upload_to=_version_upload_path, max_length=500)
    checksum_sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    original_name = models.CharField(max_length=255, blank=True)
    origin = models.CharField(max_length=12, default="uploaded")
    template_version = models.CharField(max_length=64, blank=True)
    scan_status = models.CharField(max_length=12, default="pending")
    correction_reason = models.TextField(blank=True)
    correction_diff = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "documents_document_version"
        constraints = [
            models.UniqueConstraint(fields=["document", "version"], name="uniq_document_version"),
        ]


class DocumentTemplate(TenantModel):
    """Версионируемый шаблон генерации (draft/publish/archive lifecycle, ТЗ §21.1)."""

    code = models.SlugField(max_length=63)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=24, choices=Document.Kind.choices)
    body = models.TextField()
    template_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, default="draft")
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "documents_template"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code", "template_version"], name="uniq_template_version"
            ),
        ]


class DocumentCorrectionPreference(TenantModel):
    """Замечания контрагента к формулировкам (применяются только явно, ТЗ §15.3)."""

    company = models.ForeignKey("crm.Company", on_delete=models.CASCADE, related_name="document_preferences")
    document_kind = models.CharField(max_length=24, blank=True)
    field = models.CharField(max_length=100)
    preferred_wording = models.TextField()
    approved = models.BooleanField(default=False)

    class Meta:
        db_table = "documents_correction_preference"


class ReceiptImportJob(TenantModel):
    """Импорт/распознавание квитанции (ТЗ §15.4)."""

    file_version = models.ForeignKey(
        DocumentVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    job = models.ForeignKey(
        "common.BackgroundJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    guessed_type = models.CharField(max_length=24, blank=True)
    parser_status = models.CharField(max_length=16, default="pending")
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    raw_extraction = models.JSONField(null=True, blank=True)
    warnings = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "documents_receipt_import"


class ReceiptDraft(TenantModel):
    """Черновик квитанции, подтверждаемый пользователем (ТЗ §15.4)."""

    import_job = models.OneToOneField(ReceiptImportJob, on_delete=models.CASCADE, related_name="draft")
    issuer = models.CharField(max_length=255, blank=True)
    entity = models.CharField(max_length=255, blank=True)
    trip_type = models.CharField(max_length=16, blank=True)
    segments = models.JSONField(default=list, blank=True)
    passenger_name = models.CharField(max_length=255, blank=True)
    fare = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    taxes = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fees = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    result_document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "documents_receipt_draft"
