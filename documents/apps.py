from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "documents"

    def ready(self):
        from documents.receipt_avia_brand_patch import install_receipt_avia_brand_patch
        from documents.receipt_client_pdf_finalizer import install_receipt_client_pdf_finalizer
        from documents.receipt_client_pdf_requirements import install_receipt_client_pdf_requirements_patch
        from documents.receipt_client_pdf_text_source import install_receipt_client_pdf_text_source_patch
        from documents.receipt_hotel_booking_guard import install_receipt_hotel_booking_guard
        from documents.receipt_multiform_patch import install_receipt_multiform_patch
        from documents.receipt_ocr_fallback import install_receipt_ocr_fallback
        from documents.receipt_parser_patch_safe import install_receipt_parser_patch
        from documents.receipt_pdf_grouping import install_receipt_pdf_grouping
        from documents.receipt_preflight_patch import install_receipt_preflight_patch
        from documents.receipt_problem_formats_patch import install_receipt_problem_formats_patch
        from documents.receipt_quality_guard import install_receipt_quality_guard
        from documents.receipt_recognition_engine import install_receipt_recognition_engine
        from documents.receipt_recognition_performance import install_receipt_recognition_performance_patch
        from documents.receipt_red_wings_patch import install_receipt_red_wings_patch
        from documents.receipt_rzd_fastpath import install_receipt_rzd_fastpath
        from documents.receipt_sequential_review_patch import install_receipt_sequential_review_patch
        from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
        from documents.receipt_supplier_pdf_group_fix import install_receipt_supplier_pdf_group_fix
        from documents.receipt_supplier_pdf_patch import install_receipt_supplier_pdf_patch
        from documents.receipt_supplier_pdf_writer_fix import install_receipt_supplier_pdf_writer_fix
        from documents.receipt_structural_hardening import install_receipt_structural_hardening
        from documents.receipt_tax_columns_patch import install_receipt_tax_columns_patch
        from documents.receipt_ticket_level_patch import install_receipt_ticket_level_patch

        install_receipt_parser_patch()
        install_receipt_multiform_patch()
        install_receipt_hotel_booking_guard()
        install_receipt_tax_columns_patch()
        install_receipt_avia_brand_patch()
        install_receipt_problem_formats_patch()
        install_receipt_preflight_patch()
        install_receipt_quality_guard()
        install_receipt_recognition_engine()
        install_receipt_recognition_performance_patch()
        install_receipt_ocr_fallback()
        install_receipt_rzd_fastpath()
        # Run the client-format hardening after all parser/OCR compatibility
        # wrappers so its complete segment and structured hotel data cannot be
        # collapsed again by an older fallback parser.
        install_receipt_client_pdf_requirements_patch()
        # A few supplier PDFs have malformed objects that pypdf rejects.  The
        # dedicated pdfminer wrapper is installed last among parsers so those
        # files still get their full segment and hotel structure.
        install_receipt_client_pdf_text_source_patch()
        # Normalize quirks found in the exact client samples (concatenated
        # airport codes and one-line hotel deposits) before ticket storage.
        install_receipt_client_pdf_finalizer()
        # Red Wings uses the same bilingual TCH visual form but a different text
        # extraction order: route/airport values come after both date columns and
        # ticket/issuer/date values come after their three labels. Resolve that
        # exact supplier layout after generic finalization and before storage.
        install_receipt_red_wings_patch()
        # Resolve labels and financial values from the actual PDF after all
        # supplier-specific parsers. This prevents a shared visual template
        # from inheriting the airline or amounts of the first known sample.
        install_receipt_structural_hardening()
        # Canonical post-parser grouping is the final recognition layer. It
        # splits both aviation and railway PDFs into independent child tickets,
        # deduplicates continuation pages and corrects strong rail evidence
        # before ticket-level persistence consumes the result.
        install_receipt_pdf_grouping()
        # Ticket-level storage consumes the final parser result.
        install_receipt_ticket_level_patch()
        # Run after ticket-level storage so review status/progress is preserved
        # for every child ticket and copied into document metadata.
        install_receipt_sequential_review_patch()
        # Reuse the fonts already embedded in the supplier PDF for both common
        # Type1 and Type0/CID documents before enabling financial corrections.
        install_receipt_supplier_pdf_font_codec()
        # Ensure modified PageObjects are written rather than re-cloning stale
        # pre-edit content streams from the PdfReader object graph.
        install_receipt_supplier_pdf_writer_fix()
        # Financial corrections are applied to a derived supplier PDF copy.
        # The uploaded source version remains immutable for audit/history.
        install_receipt_supplier_pdf_patch()
        # Grouped rail PDFs contain one real ticket per page.  Install this last
        # so aggregate CRM totals and derived rail aliases can never block the
        # corrected supplier copy from being produced.
        install_receipt_supplier_pdf_group_fix()
