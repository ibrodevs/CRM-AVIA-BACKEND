from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "documents"

    def ready(self):
        from documents.receipt_avia_brand_patch import install_receipt_avia_brand_patch
        from documents.receipt_hotel_booking_guard import install_receipt_hotel_booking_guard
        from documents.receipt_multiform_patch import install_receipt_multiform_patch
        from documents.receipt_parser_patch_safe import install_receipt_parser_patch
        from documents.receipt_preflight_patch import install_receipt_preflight_patch
        from documents.receipt_problem_formats_patch import install_receipt_problem_formats_patch
        from documents.receipt_quality_guard import install_receipt_quality_guard
        from documents.receipt_tax_columns_patch import install_receipt_tax_columns_patch

        install_receipt_parser_patch()
        install_receipt_multiform_patch()
        install_receipt_hotel_booking_guard()
        install_receipt_tax_columns_patch()
        install_receipt_avia_brand_patch()
        install_receipt_problem_formats_patch()
        install_receipt_preflight_patch()
        install_receipt_quality_guard()
