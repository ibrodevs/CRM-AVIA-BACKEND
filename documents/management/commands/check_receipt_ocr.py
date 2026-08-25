import json

from django.core.management.base import BaseCommand, CommandError

from documents.receipt_ocr_fallback import receipt_ocr_runtime_status


class Command(BaseCommand):
    help = "Проверяет OCR для квитанций: Tesseract, языки rus+eng и PDF renderer."

    def handle(self, *args, **options):
        status = receipt_ocr_runtime_status()
        self.stdout.write(json.dumps(status, ensure_ascii=False, indent=2))
        if not status["ready"]:
            raise CommandError(
                "OCR квитанций не готов. На PythonAnywhere выполните "
                "`python scripts/setup_pythonanywhere_ocr.py` внутри virtualenv."
            )
        self.stdout.write(self.style.SUCCESS("OCR квитанций готов."))
