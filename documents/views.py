from django.db import transaction
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from common.pagination import DefaultPagination
from documents.models import (
    Document,
    DocumentTemplate,
    ReceiptDraft,
    ReceiptImportJob,
)
from documents.selectors import documents_visible_to, get_document_or_404
from documents.serializers import DocumentSerializer, DocumentVersionSerializer
from documents.services import add_document_version, validate_upload


class DocumentListCreateView(GenericAPIView):
    permission_classes = [require("documents.view")]
    pagination_class = DefaultPagination
    serializer_class = DocumentSerializer

    def get(self, request):
        qs = documents_visible_to(request.user).order_by("-created_at")
        params = request.query_params
        if order_id := params.get("order"):
            qs = qs.filter(order_id=order_id)
        if kind := params.get("kind"):
            qs = qs.filter(kind=kind)
        if doc_status := params.get("status"):
            qs = qs.filter(status=doc_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(DocumentSerializer(page, many=True).data)

    def post(self, request):
        self.permission_classes = [require("documents.upload")]
        self.check_permissions(request)
        file = request.FILES.get("file")
        import json

        meta = request.data.get("document")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                raise ApiError(
                    code="VALIDATION_ERROR", message="document: некорректный JSON", status_code=400
                ) from None
        serializer = DocumentSerializer(data=meta or request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            document = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
            if file is not None:
                validate_upload(file)
                add_document_version(
                    document, content=file.read(), mime=file.content_type, name=file.name, user=request.user
                )
        audit("documents.uploaded", actor=request.user, resource=document, request=request)
        return Response(DocumentSerializer(document).data, status=http.HTTP_201_CREATED)


class DocumentVersionsView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        return Response(DocumentVersionSerializer(document.versions.order_by("-version"), many=True).data)

    def post(self, request, document_id):
        """Новая версия (исправление представления): diff, причина, автор (ТЗ §15.3)."""
        if not has_permission(request.user, "documents.upload") and not has_permission(
            request.user, "services.correct_document"
        ):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права на исправление", status_code=403)
        document = get_document_or_404(request.user, document_id)
        file = request.FILES.get("file")
        reason = str(request.data.get("reason", ""))
        if file is None or not reason:
            raise ApiError(code="VALIDATION_ERROR", message="Нужны file и reason", status_code=400)
        validate_upload(file)
        version = add_document_version(
            document,
            content=file.read(),
            mime=file.content_type,
            name=file.name,
            user=request.user,
            correction_reason=reason,
            correction_diff=request.data.get("diff"),
        )
        audit(
            "documents.version_added", actor=request.user, resource=document, request=request, reason=reason
        )
        return Response(DocumentVersionSerializer(version).data, status=http.HTTP_201_CREATED)


class DocumentGenerateView(APIView):
    """Генерация из версионируемого шаблона со snapshot реквизитов (ТЗ §15.3)."""

    permission_classes = [require("documents.generate")]

    def post(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        template = (
            DocumentTemplate.objects.filter(
                tenant_id=request.user.tenant_id,
                code=request.data.get("template"),
                status="published",
            )
            .order_by("-template_version")
            .first()
        )
        context = request.data.get("context", {})
        body = template.body if template else "{title}\n{context}"
        rendered = body.format(title=document.title, context=context)
        version = add_document_version(
            document,
            content=rendered.encode(),
            mime="text/plain",
            name=f"{document.title}.txt",
            user=request.user,
            origin="generated",
            template_version=f"{template.code}:{template.template_version}" if template else "",
        )
        emit_event(
            "order.updated",
            document.order or document,
            payload={"action": "document_generated", "document": str(document.id)},
        )
        audit("documents.generated", actor=request.user, resource=document, request=request)
        return Response(DocumentVersionSerializer(version).data, status=http.HTTP_201_CREATED)


class DocumentSignView(APIView):
    """Подпись: на первом этапе статус + внешний reference через адаптер (ТЗ §15.4)."""

    permission_classes = [require("documents.sign")]

    def post(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        if document.current_version == 0:
            raise ApiError(code="NO_VERSION", message="Нет версии для подписания", status_code=409)
        if document.status == Document.Status.VOID:
            raise ApiError(code="DOCUMENT_VOID", message="Документ аннулирован", status_code=409)
        document.status = Document.Status.SIGNED
        document.metadata = {
            **document.metadata,
            "signature_reference": str(request.data.get("reference", "")),
            "signed_at": timezone.now().isoformat(),
            "signed_by": str(request.user.id),
        }
        document.save(update_fields=["status", "metadata"])
        audit("documents.signed", actor=request.user, resource=document, request=request)
        return Response(DocumentSerializer(document).data)


class DocumentVoidView(APIView):
    permission_classes = [require("documents.void")]

    def post(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        reason = str(request.data.get("reason", ""))
        if not reason:
            raise ApiError(code="REASON_REQUIRED", message="Аннулирование требует причины", status_code=400)
        document.status = Document.Status.VOID
        document.metadata = {**document.metadata, "void_reason": reason}
        document.save(update_fields=["status", "metadata"])
        audit("documents.voided", actor=request.user, resource=document, request=request, reason=reason)
        return Response(DocumentSerializer(document).data)


class DocumentSendView(APIView):
    permission_classes = [require("documents.send")]

    def post(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        if document.current_version == 0:
            raise ApiError(code="NO_VERSION", message="Нет файла для отправки", status_code=409)
        channel = str(request.data.get("channel", "email"))
        emit_event(
            "order.updated",
            document.order or document,
            payload={"action": "document_sent", "document": str(document.id), "channel": channel},
        )
        audit(
            "documents.sent",
            actor=request.user,
            resource=document,
            request=request,
            after={"channel": channel},
        )
        return Response({"status": "queued", "channel": channel})


class DocumentDownloadView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request, document_id):
        document = get_document_or_404(request.user, document_id)
        version_number = request.query_params.get("file_version")
        version = (
            document.versions.filter(version=version_number).first()
            if version_number
            else document.versions.order_by("-version").first()
        )
        if version is None:
            raise ApiError(code="NO_VERSION", message="Нет файла", status_code=404)
        if version.scan_status != "clean":
            raise ApiError(code="FILE_QUARANTINED", message="Файл не прошёл проверку", status_code=423)

        if document.is_confidential:
            audit("documents.sensitive_downloaded", actor=request.user, resource=document, request=request)
        response = FileResponse(version.file.open("rb"), content_type=version.mime_type)
        response["Content-Disposition"] = f'attachment; filename="{version.original_name or document.title}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DocumentTemplatesView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request):
        templates = DocumentTemplate.objects.filter(
            tenant_id=request.user.tenant_id, archived_at__isnull=True
        )
        return Response(
            [
                {
                    "id": str(t.id),
                    "code": t.code,
                    "name": t.name,
                    "kind": t.kind,
                    "template_version": t.template_version,
                    "status": t.status,
                }
                for t in templates
            ]
        )

    def post(self, request):
        self.permission_classes = [require("settings.manage")]
        self.check_permissions(request)
        code = str(request.data.get("code", "")).strip()
        if not code:
            raise ApiError(code="VALIDATION_ERROR", message="code обязателен", status_code=400)
        last = (
            DocumentTemplate.objects.filter(tenant_id=request.user.tenant_id, code=code)
            .order_by("-template_version")
            .first()
        )
        template = DocumentTemplate.objects.create(
            tenant_id=request.user.tenant_id,
            code=code,
            name=str(request.data.get("name", code)),
            kind=str(request.data.get("kind", "other")),
            body=str(request.data.get("body", "")),
            template_version=(last.template_version + 1) if last else 1,
            status="published" if request.data.get("publish") else "draft",
            published_at=timezone.now() if request.data.get("publish") else None,
            created_by=request.user,
        )
        audit("documents.template_created", actor=request.user, resource=template, request=request)
        return Response(
            {"id": str(template.id), "template_version": template.template_version},
            status=http.HTTP_201_CREATED,
        )


class ReceiptImportCreateView(APIView):
    permission_classes = [require("documents.upload")]

    def post(self, request):
        file = request.FILES.get("file")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл file обязателен", status_code=400)
        validate_upload(file)
        import_job = ReceiptImportJob.objects.create(
            tenant_id=request.user.tenant_id,
            created_by=request.user,
            guessed_type="itinerary_receipt",
            parser_status="parsed",
            confidence="0.500",
            raw_extraction={"note": "OCR-адаптер не подключён; заполните поля вручную"},
            warnings=["OCR выполняется заглушкой; проверьте все поля"],
        )
        ReceiptDraft.objects.create(
            tenant_id=request.user.tenant_id,
            import_job=import_job,
            created_by=request.user,
        )
        return Response({"id": str(import_job.id)}, status=http.HTTP_201_CREATED)


class ReceiptImportResultView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request, import_id):
        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        if import_job is None:
            raise ApiError(code="NOT_FOUND", message="Импорт не найден", status_code=404)
        draft = getattr(import_job, "draft", None)
        return Response(
            {
                "id": str(import_job.id),
                "parser_status": import_job.parser_status,
                "confidence": str(import_job.confidence) if import_job.confidence else None,
                "warnings": import_job.warnings,
                "draft": {
                    "issuer": draft.issuer,
                    "entity": draft.entity,
                    "trip_type": draft.trip_type,
                    "segments": draft.segments,
                    "passenger_name": draft.passenger_name,
                    "fare": str(draft.fare) if draft.fare else None,
                    "taxes": str(draft.taxes) if draft.taxes else None,
                    "fees": str(draft.fees) if draft.fees else None,
                    "total": str(draft.total) if draft.total else None,
                    "currency": draft.currency,
                }
                if draft
                else None,
            }
        )


class ReceiptImportConfirmView(APIView):
    """Пользователь подтверждает поля; сервер пересчитывает итог (ТЗ §15.4)."""

    permission_classes = [require("documents.upload")]

    def post(self, request, import_id):
        from decimal import Decimal

        from common.money import quantize

        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        if import_job is None:
            raise ApiError(code="NOT_FOUND", message="Импорт не найден", status_code=404)
        draft = getattr(import_job, "draft", None)
        if draft is None or draft.confirmed_at is not None:
            raise ApiError(code="ALREADY_CONFIRMED", message="Черновик уже подтверждён", status_code=409)
        data = request.data
        currency = str(data.get("currency", "USD"))
        fare = Decimal(str(data.get("fare", "0")))
        taxes = Decimal(str(data.get("taxes", "0")))
        fees = Decimal(str(data.get("fees", "0")))
        total = quantize(fare + taxes + fees, currency)
        with transaction.atomic():
            draft.issuer = str(data.get("issuer", ""))
            draft.passenger_name = str(data.get("passenger_name", ""))
            draft.segments = data.get("segments", [])
            draft.fare, draft.taxes, draft.fees = fare, taxes, fees
            draft.total = total
            draft.currency = currency
            draft.confirmed_at = timezone.now()
            document = Document.objects.create(
                tenant_id=request.user.tenant_id,
                kind="itinerary_receipt",
                title=f"Квитанция {draft.passenger_name or ''}".strip(),
                source="import",
                amount=total,
                currency=currency,
                created_by=request.user,
            )
            draft.result_document = document
            draft.save()
            content = (
                f"RECEIPT\nPassenger: {draft.passenger_name}\n"
                f"Fare: {fare} Taxes: {taxes} Fees: {fees}\n"
                f"Total: {total} {currency}\n"
            ).encode()
            add_document_version(
                document,
                content=content,
                mime="text/plain",
                name="receipt.txt",
                user=request.user,
                origin="generated",
            )
        audit("documents.receipt_confirmed", actor=request.user, resource=document, request=request)
        return Response({"document_id": str(document.id), "total": str(total), "currency": currency})
