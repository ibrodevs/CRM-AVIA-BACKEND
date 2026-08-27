"""Эндпоинт распознавания документа личности.

Ничего не сохраняет: возвращает поля для подстановки в карточку физлица,
чтобы OCR был именно альтернативой ручному вводу анкеты.
"""

from __future__ import annotations

from rest_framework.response import Response
from rest_framework.views import APIView

from common.errors import ApiError
from accounts.permissions import require
from crm.person_document_ocr import recognize_person_document
from documents.services import validate_upload


class PersonDocumentRecognizeView(APIView):
    permission_classes = [require("crm.change")]

    def post(self, request):
        file = request.FILES.get("file")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл документа не передан", status_code=400)
        validate_upload(file)
        content = file.read()
        result = recognize_person_document(content, mime=file.content_type or "")
        result["file_name"] = file.name
        return Response(result)
