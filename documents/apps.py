from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "documents"

    def ready(self):
        from documents.receipt_parser_patch_safe import install_receipt_parser_patch
        from documents.receipt_tax_columns_patch import install_receipt_tax_columns_patch

        install_receipt_parser_patch()
        install_receipt_tax_columns_patch()
