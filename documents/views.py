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
from documents.receipt_metadata import json_safe, receipt_document_metadata, receipt_verified_data
from documents.selectors import documents_visible_to, get_document_or_404
from documents.serializers import DocumentSerializer, DocumentVersionSerializer
from documents.services import add_document_version, extract_receipt_fields, validate_upload


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
        serializer = DocumentSerializer(data=meta or request.data, context={"request": request})
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
        disposition = "inline" if request.query_params.get("disposition") == "inline" else "attachment"
        response["Content-Disposition"] = (
            f'{disposition}; filename="{version.original_name or document.title}"'
        )
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DocumentReceiptUpdateView(APIView):
    """Сохраняет проверенные данные редактора, не изменяя оригинал поставщика."""

    permission_classes = [require("documents.upload")]

    def post(self, request, document_id):
        from decimal import Decimal, InvalidOperation

        document = get_document_or_404(request.user, document_id)
        verified_input = request.data.get("verified_data")
        if not isinstance(verified_input, dict):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="verified_data должен быть объектом",
                status_code=400,
            )

        if "order" in request.data:
            order_id = request.data.get("order")
            if order_id:
                from orders.models import Order

                order = Order.objects.filter(
                    pk=order_id,
                    tenant_id=request.user.tenant_id,
                ).first()
                if order is None:
                    raise ApiError(code="NOT_FOUND", message="Заказ не найден", status_code=404)
                document.order = order
            else:
                document.order = None

        if "person" in request.data:
            person_id = request.data.get("person")
            if person_id:
                from crm.models import Person

                person = Person.objects.filter(
                    pk=person_id,
                    tenant_id=request.user.tenant_id,
                ).first()
                if person is None:
                    raise ApiError(code="NOT_FOUND", message="Физлицо не найдено", status_code=404)
                document.person = person
            else:
                document.person = None

        save_as_draft = bool(request.data.get("draft"))
        verified = receipt_verified_data(
            verified_input,
            parser_status="manual_review" if save_as_draft else "parsed",
        )
        verified["recognitionPending"] = save_as_draft
        output_settings = request.data.get("output_settings")
        audit_log = request.data.get("audit_log")
        current = document.metadata or {}
        supplier_original = {
            **(current.get("supplier_original") or {}),
            "verified_data": json_safe(verified),
        }
        if isinstance(output_settings, dict):
            supplier_original["output_settings"] = json_safe(output_settings)
        if isinstance(audit_log, list):
            supplier_original["audit_log"] = json_safe(audit_log)
        receipt_import = {
            **(current.get("receipt_import") or {}),
            "stage": "draft" if save_as_draft else "confirmed",
            "verified_data": json_safe(verified),
        }
        document.metadata = {
            **current,
            "supplier_original": supplier_original,
            "receipt_import": receipt_import,
        }

        total = verified.get("total")
        if total not in (None, ""):
            try:
                document.amount = Decimal(str(total))
            except (InvalidOperation, TypeError, ValueError):
                raise ApiError(
                    code="VALIDATION_ERROR",
                    message="Некорректная итоговая сумма",
                    status_code=400,
                ) from None
        document.currency = str(verified.get("currency") or document.currency or "")
        document.source = "corrected"
        document.save(update_fields=["order", "person", "amount", "currency", "source", "metadata"])
        audit(
            "documents.receipt_draft_saved" if save_as_draft else "documents.receipt_updated",
            actor=request.user,
            resource=document,
            request=request,
        )
        return Response(DocumentSerializer(document).data)


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
        content = file.read()
        extraction = extract_receipt_fields(content, mime=file.content_type, name=file.name)
        fields = extraction["fields"]
        with transaction.atomic():
            document = Document.objects.create(
                tenant_id=request.user.tenant_id,
                kind="itinerary_receipt",
                title=file.name,
                source="supplier",
                metadata={
                    "supplier_original": {
                        "name": file.name,
                        "mime": file.content_type,
                        "size": file.size,
                    },
                    "receipt_import": {"stage": "uploaded"},
                },
                created_by=request.user,
            )
            original_version = add_document_version(
                document,
                content=content,
                mime=file.content_type,
                name=file.name,
                user=request.user,
                origin="uploaded",
            )
            import_job = ReceiptImportJob.objects.create(
                tenant_id=request.user.tenant_id,
                created_by=request.user,
                file_version=original_version,
                guessed_type=fields.get("service_kind") or "other",
                parser_status=extraction["status"],
                confidence=extraction["confidence"],
                raw_extraction=extraction["raw"],
                warnings=extraction["warnings"],
            )
            ReceiptDraft.objects.create(
                tenant_id=request.user.tenant_id,
                import_job=import_job,
                created_by=request.user,
                issuer=fields.get("issuer") or "",
                passenger_name=fields.get("passenger_name") or "",
                fare=fields.get("fare"),
                taxes=fields.get("taxes"),
                fees=fields.get("fees"),
                fare_breakdown=fields.get("fare_breakdown") or [],
                tax_breakdown=fields.get("tax_breakdown") or [],
                fee_breakdown=fields.get("fee_breakdown") or [],
                total=fields.get("total"),
                currency=fields.get("currency") or "",
                segments=fields.get("segments") or [],
                trip_type=fields.get("trip_type") or "",
            )
            document.metadata = receipt_document_metadata(
                document.metadata,
                import_id=import_job.id,
                extraction=extraction,
                file_name=file.name,
                mime=file.content_type,
                size=file.size,
            )
            document.save(update_fields=["metadata"])
        return Response(
            {
                "id": str(import_job.id),
                "document_id": str(original_version.document_id),
            },
            status=http.HTTP_201_CREATED,
        )


class ReceiptImportResultView(APIView):
    permission_classes = [require("documents.view")]

    def get(self, request, import_id):
        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        if import_job is None:
            raise ApiError(code="NOT_FOUND", message="Импорт не найден", status_code=404)
        draft = getattr(import_job, "draft", None)
        raw_extraction = import_job.raw_extraction or {}
        verified_source = {
            **raw_extraction,
            **(
                {
                    "issuer": draft.issuer,
                    "passenger_name": draft.passenger_name,
                    "fare": draft.fare,
                    "taxes": draft.taxes,
                    "fees": draft.fees,
                    "fare_breakdown": draft.fare_breakdown,
                    "tax_breakdown": draft.tax_breakdown,
                    "fee_breakdown": draft.fee_breakdown,
                    "total": draft.total,
                    "currency": draft.currency,
                    "segments": draft.segments,
                    "trip_type": draft.trip_type,
                }
                if draft
                else {}
            ),
        }
        verified_data = receipt_verified_data(
            verified_source,
            parser_status=import_job.parser_status,
        )
        return Response(
            {
                "id": str(import_job.id),
                "source_document_id": (
                    str(import_job.file_version.document_id)
                    if import_job.file_version_id
                    else None
                ),
                "parser_status": import_job.parser_status,
                "confidence": str(import_job.confidence) if import_job.confidence else None,
                "warnings": import_job.warnings,
                "extracted": {
                    **raw_extraction,
                    "reference": (import_job.raw_extraction or {}).get("reference", ""),
                    "ticket_number": (import_job.raw_extraction or {}).get("ticket_number", ""),
                    "document_number": (import_job.raw_extraction or {}).get("document_number", ""),
                    "date_of_birth": (import_job.raw_extraction or {}).get("date_of_birth", ""),
                    "issue_date": (import_job.raw_extraction or {}).get("issue_date", ""),
                    "booking_class": (import_job.raw_extraction or {}).get("booking_class", ""),
                    "fare_basis": (import_job.raw_extraction or {}).get("fare_basis", ""),
                    "baggage": (import_job.raw_extraction or {}).get("baggage", ""),
                    "hand_baggage": (import_job.raw_extraction or {}).get("hand_baggage", ""),
                    "segments": (import_job.raw_extraction or {}).get("segments", []),
                    "trip_type": (import_job.raw_extraction or {}).get("trip_type", ""),
                    "fare_breakdown": (import_job.raw_extraction or {}).get("fare_breakdown", []),
                    "tax_breakdown": (import_job.raw_extraction or {}).get("tax_breakdown", []),
                    "fee_breakdown": (import_job.raw_extraction or {}).get("fee_breakdown", []),
                    "service_kind": (import_job.raw_extraction or {}).get("service_kind", import_job.guessed_type),
                    "service_type": (import_job.raw_extraction or {}).get("service_type", ""),
                },
                "verified_data": verified_data,
                "draft": {
                    "issuer": draft.issuer,
                    "entity": draft.entity,
                    "trip_type": draft.trip_type,
                    "segments": draft.segments,
                    "passenger_name": draft.passenger_name,
                    "fare": str(draft.fare) if draft.fare else None,
                    "taxes": str(draft.taxes) if draft.taxes else None,
                    "fees": str(draft.fees) if draft.fees else None,
                    "fare_breakdown": draft.fare_breakdown,
                    "tax_breakdown": draft.tax_breakdown,
                    "fee_breakdown": draft.fee_breakdown,
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
        order = None
        if order_id := data.get("order"):
            from orders.models import Order

            order = Order.objects.filter(pk=order_id, tenant_id=request.user.tenant_id).first()
            if order is None:
                raise ApiError(code="NOT_FOUND", message="Заказ не найден", status_code=404)
        currency = str(data.get("currency", "USD"))
        fare = Decimal(str(data.get("fare", "0")))
        taxes = Decimal(str(data.get("taxes", "0")))
        fees = Decimal(str(data.get("fees", "0")))
        total = quantize(fare + taxes + fees, currency)
        with transaction.atomic():
            draft.issuer = str(data.get("issuer", ""))
            draft.passenger_name = str(data.get("passenger_name", ""))
            draft.segments = data.get("segments", [])
            draft.trip_type = str(data.get("trip_type", draft.trip_type or ""))
            draft.fare, draft.taxes, draft.fees = fare, taxes, fees
            draft.fare_breakdown = data.get("fare_breakdown", draft.fare_breakdown or [])
            draft.tax_breakdown = data.get("tax_breakdown", draft.tax_breakdown or [])
            draft.fee_breakdown = data.get("fee_breakdown", draft.fee_breakdown or [])
            draft.total = total
            draft.currency = currency
            draft.confirmed_at = timezone.now()
            raw_service_kind = str(
                (import_job.raw_extraction or {}).get("service_kind", import_job.guessed_type)
                or ""
            ).strip().lower()
            normalized_service_kind = {
                "авиа": "avia",
                "flight": "avia",
                "жд": "rail",
                "ж/д": "rail",
                "train": "rail",
                "гостиница": "hotel",
                "отель": "hotel",
                "трансфер": "transfer",
            }.get(raw_service_kind, raw_service_kind or "other")
            document_kind = {
                "avia": "itinerary_receipt",
                "rail": "ticket",
                "hotel": "voucher",
                "transfer": "voucher",
            }.get(normalized_service_kind, "other")
            document_label = {
                "avia": "Маршрут-квитанция",
                "rail": "Электронный ЖД-билет",
                "hotel": "Ваучер отеля",
                "transfer": "Ваучер трансфера",
            }.get(normalized_service_kind, "Документ услуги")
            document_title = " · ".join(
                part for part in (document_label, draft.passenger_name or draft.issuer) if part
            )
            source_document = import_job.file_version.document if import_job.file_version_id else None
            document = source_document or Document.objects.create(
                tenant_id=request.user.tenant_id,
                kind=document_kind,
                title=document_title,
                source="supplier",
                created_by=request.user,
            )
            document.order = order
            document.kind = document_kind
            document.title = document_title
            document.source = "corrected"
            document.amount = total
            document.currency = currency
            document.metadata = {
                **(document.metadata or {}),
                "supplier_original": {
                    **((document.metadata or {}).get("supplier_original") or {}),
                    **json_safe(data.get("supplier_original") or {}),
                },
                "receipt_import": {
                    **((document.metadata or {}).get("receipt_import") or {}),
                    "stage": "confirmed",
                    "import_id": str(import_job.id),
                    "parser_status": import_job.parser_status,
                    "warnings": import_job.warnings,
                    "service_kind": normalized_service_kind,
                    "service_type": (import_job.raw_extraction or {}).get("service_type", ""),
                    "original_total": str(data.get("original_total", draft.total or 0)),
                    "client_total": str(data.get("client_total", total)),
                    "markup": str(data.get("markup", 0)),
                    "commission": str(data.get("commission", 0)),
                    "corrected_fields": {
                        "issuer": draft.issuer,
                        "passenger_name": draft.passenger_name,
                        "segments": draft.segments,
                        "trip_type": draft.trip_type,
                        "fare": str(fare),
                        "taxes": str(taxes),
                        "fees": str(fees),
                        "fare_breakdown": draft.fare_breakdown,
                        "tax_breakdown": draft.tax_breakdown,
                        "fee_breakdown": draft.fee_breakdown,
                        "total": str(total),
                        "currency": currency,
                    },
                },
            }
            submitted_verified = (
                (data.get("supplier_original") or {}).get("verified_data")
                if isinstance(data.get("supplier_original"), dict)
                else None
            )
            verified_data = receipt_verified_data(
                submitted_verified
                or {
                    **(import_job.raw_extraction or {}),
                    **document.metadata["receipt_import"]["corrected_fields"],
                },
                parser_status=import_job.parser_status,
            )
            verified_data["recognitionPending"] = False
            verified_data["manualCompletion"] = import_job.parser_status != "parsed"
            document.metadata["supplier_original"]["verified_data"] = json_safe(verified_data)
            document.metadata["receipt_import"]["verified_data"] = json_safe(verified_data)
            if order is not None and data.get("create_services", True):
                from services.models import OrderService

                service_type = str(data.get("service_type", "avia"))
                kind_map = {
                    "Авиа": "avia",
                    "ЖД": "rail",
                    "Гостиница": "hotel",
                    "Трансфер": "transfer",
                    "Автобус": "bus",
                    "Тур": "tour",
                    "Страхование": "insurance",
                    "Виза": "visa",
                    "Прочее": "other",
                }
                service = OrderService.objects.create(
                    tenant_id=request.user.tenant_id,
                    order=order,
                    kind=kind_map.get(service_type, service_type if service_type in kind_map.values() else "other"),
                    status=OrderService.Status.ISSUED,
                    title=(draft.issuer or service_type or "Услуга") + (
                        f" · {draft.passenger_name}" if draft.passenger_name else ""
                    ),
                    source=OrderService.Source.IMPORT,
                    supplier_cost=quantize(fare + taxes, currency),
                    agency_fee=fees,
                    markup=Decimal(str(data.get("markup", "0"))),
                    commission=Decimal(str(data.get("commission", "0"))),
                    client_total=Decimal(str(data.get("client_total", total))),
                    currency=currency,
                    created_by=request.user,
                    updated_by=request.user,
                )
                document.service = service
                document.metadata["receipt_import"]["created_service"] = str(service.id)
            document.save(update_fields=["order", "service", "kind", "title", "source", "amount", "currency", "metadata"])
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
                correction_reason="Подтверждённые данные после импорта; оригинал поставщика сохранён в v1",
                correction_diff=document.metadata["receipt_import"]["corrected_fields"],
            )
        audit("documents.receipt_confirmed", actor=request.user, resource=document, request=request)
        return Response({"document_id": str(document.id), "total": str(total), "currency": currency})
