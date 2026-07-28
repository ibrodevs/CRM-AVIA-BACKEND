from django.core.management.base import BaseCommand
from django.db import transaction

from documents import services
from documents.models import ReceiptImportJob
from documents.receipt_metadata import receipt_document_metadata
from documents.receipt_parser_patch_safe import install_receipt_parser_patch


class Command(BaseCommand):
    help = "Повторно распознаёт неподтверждённые квитанции из сохранённых оригиналов."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Включить уже распознанные задания.")
        parser.add_argument("--dry-run", action="store_true", help="Не сохранять изменения.")

    def handle(self, *args, **options):
        # Management commands do not necessarily load ``documents.urls``, where
        # the supplier-layout parser is installed for HTTP requests.  Install it
        # here as well and resolve the function from the module at call time so
        # reprocessing uses exactly the same parser as a fresh upload.
        install_receipt_parser_patch()
        jobs = (
            ReceiptImportJob.objects.select_related("file_version__document", "draft")
            .filter(file_version__isnull=False, draft__confirmed_at__isnull=True)
            .order_by("created_at")
        )
        if not options["all"]:
            jobs = jobs.exclude(parser_status="parsed")

        stats = {"parsed": 0, "manual_review": 0, "errors": 0}
        for job in jobs.iterator():
            version = job.file_version
            try:
                with version.file.open("rb") as source:
                    content = source.read()
                extraction = services.extract_receipt_fields(
                    content,
                    mime=version.mime_type,
                    name=version.original_name or version.file.name.rsplit("/", 1)[-1],
                )
                status = extraction.get("status") or "manual_review"
                stats[status if status in stats else "manual_review"] += 1
                if options["dry_run"]:
                    continue

                fields = extraction.get("fields") or {}
                with transaction.atomic():
                    job.guessed_type = fields.get("service_kind") or "other"
                    job.parser_status = status
                    job.confidence = extraction.get("confidence")
                    job.raw_extraction = extraction.get("raw") or {}
                    job.warnings = extraction.get("warnings") or []
                    job.save(
                        update_fields=[
                            "guessed_type",
                            "parser_status",
                            "confidence",
                            "raw_extraction",
                            "warnings",
                            "updated_at",
                        ]
                    )

                    draft = job.draft
                    draft.issuer = fields.get("issuer") or ""
                    draft.passenger_name = fields.get("passenger_name") or ""
                    draft.fare = fields.get("fare")
                    draft.taxes = fields.get("taxes")
                    draft.fees = fields.get("fees")
                    draft.tax_breakdown = fields.get("tax_breakdown") or []
                    draft.fee_breakdown = fields.get("fee_breakdown") or []
                    draft.total = fields.get("total")
                    draft.currency = fields.get("currency") or ""
                    draft.segments = fields.get("segments") or []
                    draft.trip_type = fields.get("trip_type") or ""
                    draft.save()

                    document = version.document
                    document.metadata = receipt_document_metadata(
                        document.metadata,
                        import_id=job.id,
                        extraction=extraction,
                        file_name=version.original_name or document.title,
                        mime=version.mime_type,
                        size=version.size_bytes,
                    )
                    document.amount = fields.get("total")
                    document.currency = fields.get("currency") or ""
                    document.save(update_fields=["metadata", "amount", "currency", "updated_at"])
            except Exception as error:  # noqa: BLE001 - continue processing independent originals
                stats["errors"] += 1
                self.stderr.write(f"{job.id}: {error}")

        prefix = "DRY RUN · " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}parsed={stats['parsed']} manual_review={stats['manual_review']} "
                f"errors={stats['errors']}"
            )
        )
