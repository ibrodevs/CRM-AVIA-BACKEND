"""Groups API (ТЗ §11)."""
import csv
import io

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers, status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError, TransitionForbiddenError
from common.outbox import emit_event
from common.pagination import DefaultPagination
from crm.models import Person
from groups_app.models import (
    GROUP_ORDER_TRANSITIONS, GroupBlock, GroupOrder, GroupPassengerAssignment,
    GroupRequest, GroupSupplierResponse, PassengerGroup, RosterImportJob,
    RosterMergeHistory,
)
from groups_app.roster import build_preview, parse_file, transliterate, validate_row
from orders.models import OrderParticipant
from orders.views import get_order_or_404


class PassengerGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PassengerGroup
        fields = ["id", "type", "company", "name", "owner", "created_at"]
        read_only_fields = ["id", "created_at"]


class GroupBlockSerializer(serializers.ModelSerializer):
    assigned_count = serializers.SerializerMethodField()

    class Meta:
        model = GroupBlock
        fields = ["id", "name", "seats", "fare_amount", "fare_currency", "details",
                  "assigned_count"]
        read_only_fields = ["id"]

    def get_assigned_count(self, obj) -> int:
        return obj.assignments.exclude(status__in=["replaced", "removed"]).count()


class GroupOrderSerializer(serializers.ModelSerializer):
    blocks = GroupBlockSerializer(many=True, read_only=True)
    order_number = serializers.CharField(source="order.number", read_only=True)

    class Meta:
        model = GroupOrder
        fields = ["id", "order", "order_number", "group", "scenario", "airline",
                  "supplier", "requested_seats", "confirmed_seats", "deposit_deadline",
                  "names_deadline", "status", "split_state", "blocks", "version"]
        read_only_fields = ["id", "status", "version"]


class PassengerGroupListCreateView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = PassengerGroupSerializer

    def get(self, request):
        qs = PassengerGroup.objects.filter(tenant_id=request.user.tenant_id,
                                           archived_at__isnull=True)
        page = self.paginate_queryset(qs.order_by("name"))
        return self.get_paginated_response(PassengerGroupSerializer(page, many=True).data)

    def post(self, request):
        serializer = PassengerGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        group = serializer.save(tenant_id=request.user.tenant_id,
                                owner=request.user, created_by=request.user)
        return Response(PassengerGroupSerializer(group).data, status=http.HTTP_201_CREATED)


class GroupOrderListCreateView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = GroupOrderSerializer

    def get(self, request):
        qs = GroupOrder.objects.filter(tenant_id=request.user.tenant_id,
                                       archived_at__isnull=True).select_related("order")
        if group_status := request.query_params.get("status"):
            qs = qs.filter(status=group_status)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response(GroupOrderSerializer(page, many=True).data)

    def post(self, request):
        self.permission_classes = [require("orders.create")]
        self.check_permissions(request)
        serializer = GroupOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_order_or_404(request.user, serializer.validated_data["order"].pk)
        group_order = serializer.save(tenant_id=request.user.tenant_id,
                                      created_by=request.user)
        order.is_group = True
        order.save(update_fields=["is_group"])
        audit("groups.group_order_created", actor=request.user, resource=group_order,
              request=request)
        return Response(GroupOrderSerializer(group_order).data,
                        status=http.HTTP_201_CREATED)


def _get_group_order(request, group_order_id) -> GroupOrder:
    group_order = GroupOrder.objects.filter(pk=group_order_id,
                                            tenant_id=request.user.tenant_id).first()
    if group_order is None:
        raise ApiError(code="NOT_FOUND", message="Групповой заказ не найден",
                       status_code=404)
    return group_order


class GroupOrderDetailView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, group_order_id):
        return Response(GroupOrderSerializer(_get_group_order(request, group_order_id)).data)


class GroupOrderTransitionView(APIView):
    permission_classes = [require("orders.change_status")]

    def post(self, request, group_order_id):
        target = str(request.data.get("target_status", ""))
        with transaction.atomic():
            group_order = GroupOrder.objects.select_for_update().get(
                pk=_get_group_order(request, group_order_id).pk
            )
            allowed = GROUP_ORDER_TRANSITIONS.get(group_order.status, set())
            if target not in allowed:
                raise TransitionForbiddenError(
                    code="GROUP_ORDER_TRANSITION_FORBIDDEN",
                    message=f"Переход из {group_order.status} в {target} запрещён",
                    details={"current_status": group_order.status,
                             "allowed": sorted(allowed)},
                )
            old = group_order.status
            group_order.status = target
            group_order.version += 1
            group_order.updated_by = request.user
            group_order.save(update_fields=["status", "version", "updated_by",
                                            "updated_at"])
            emit_event("order.updated", group_order.order,
                       payload={"action": "group_status", "to": target})
            audit("groups.status_changed", actor=request.user, resource=group_order,
                  request=request, before={"status": old}, after={"status": target})
        return Response(GroupOrderSerializer(group_order).data)


class GroupBlocksView(APIView):
    permission_classes = [require("orders.change")]

    def get(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        return Response(GroupBlockSerializer(group_order.blocks.all(), many=True).data)

    def post(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        serializer = GroupBlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        block = serializer.save(tenant_id=group_order.tenant_id, group_order=group_order,
                                created_by=request.user)
        return Response(GroupBlockSerializer(block).data, status=http.HTTP_201_CREATED)


class GroupMatrixView(APIView):
    """Матрица пассажиры x блоки с назначениями."""

    permission_classes = [require("orders.view")]

    def get(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        participants = group_order.order.participants.filter(
            status="active").select_related("person")
        blocks = list(group_order.blocks.all())
        assignments = GroupPassengerAssignment.objects.filter(
            block__group_order=group_order
        ).exclude(status__in=["replaced", "removed"])
        assignment_map = {(a.participant_id, a.block_id): a for a in assignments}
        rows = []
        for participant in participants:
            cells = []
            for block in blocks:
                assignment = assignment_map.get((participant.pk, block.pk))
                cells.append({
                    "block_id": str(block.pk),
                    "assigned": assignment is not None,
                    "status": assignment.status if assignment else None,
                    "seat": assignment.seat_number if assignment else "",
                })
            rows.append({
                "participant_id": str(participant.pk),
                "name": participant.person.full_name if participant.person
                else (participant.guest_snapshot or {}).get("surname", ""),
                "subgroup": participant.subgroup_name,
                "cells": cells,
            })
        return Response({
            "blocks": GroupBlockSerializer(blocks, many=True).data,
            "rows": rows,
        })


class GroupMassActionView(APIView):
    """Массовые операции: per-item результат, частичный успех не теряется (ТЗ §11)."""

    permission_classes = [require("orders.change")]

    ACTIONS = {"assign_block", "set_seat", "set_baggage", "set_fare", "validate",
               "remove"}

    def post(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        action = str(request.data.get("action", ""))
        items = request.data.get("items", [])
        if action not in self.ACTIONS or not isinstance(items, list):
            raise ApiError(code="VALIDATION_ERROR",
                           message=f"action из {sorted(self.ACTIONS)} и список items",
                           status_code=400)
        results = []
        for item in items:
            participant_id = item.get("participant_id")
            try:
                with transaction.atomic():
                    results.append(self._apply(group_order, action, item, request))
            except ApiError as exc:
                results.append({"participant_id": participant_id, "status": "error",
                                "code": exc.code, "message": exc.message})
            except Exception as exc:  # noqa: BLE001 — per-item изоляция ошибок
                results.append({"participant_id": participant_id, "status": "error",
                                "code": "INTERNAL", "message": str(exc)[:200]})
        ok_count = sum(1 for r in results if r["status"] == "ok")
        audit("groups.mass_action", actor=request.user, resource=group_order,
              request=request, after={"action": action, "ok": ok_count,
                                      "failed": len(results) - ok_count})
        return Response({"action": action, "results": results,
                         "summary": {"ok": ok_count, "failed": len(results) - ok_count}})

    def _apply(self, group_order, action, item, request) -> dict:
        participant = OrderParticipant.objects.filter(
            pk=item.get("participant_id"), order=group_order.order, status="active"
        ).first()
        if participant is None:
            raise ApiError(code="PARTICIPANT_NOT_FOUND", message="Участник не найден")
        if action == "assign_block":
            block = group_order.blocks.filter(pk=item.get("block_id")).first()
            if block is None:
                raise ApiError(code="BLOCK_NOT_FOUND", message="Блок не найден")
            active = block.assignments.exclude(status__in=["replaced", "removed"])
            if active.count() >= block.seats:
                raise ApiError(code="BLOCK_FULL", message="Блок заполнен")
            conflicting = GroupPassengerAssignment.objects.filter(
                participant=participant, block__group_order=group_order,
            ).exclude(status__in=["replaced", "removed"]).exclude(block=block)
            if conflicting.exists():
                raise ApiError(code="CONFLICTING_BLOCK",
                               message="Пассажир уже в другом блоке")
            GroupPassengerAssignment.objects.get_or_create(
                block=block, participant=participant,
                defaults={"tenant_id": group_order.tenant_id,
                          "created_by": request.user},
            )
        else:
            assignment = GroupPassengerAssignment.objects.filter(
                participant=participant, block__group_order=group_order,
            ).exclude(status__in=["replaced", "removed"]).first()
            if assignment is None:
                raise ApiError(code="NOT_ASSIGNED", message="Пассажир не в блоке")
            if action == "set_seat":
                seat = str(item.get("seat", ""))
                duplicate = assignment.block.assignments.filter(seat_number=seat).exclude(
                    pk=assignment.pk).exclude(status__in=["replaced", "removed"])
                if seat and duplicate.exists():
                    raise ApiError(code="SEAT_TAKEN", message=f"Место {seat} занято")
                assignment.seat_number = seat
            elif action == "set_baggage":
                assignment.baggage = str(item.get("baggage", ""))
            elif action == "set_fare":
                assignment.fare_code = str(item.get("fare_code", ""))
            elif action == "validate":
                person = participant.person
                row = {
                    "surname": person.surname if person else "",
                    "given_name": person.given_name if person else "",
                    "latin_surname": person.latin_surname if person else "",
                    "latin_given_name": person.latin_given_name if person else "",
                    "birth_date": str(person.birth_date) if person and person.birth_date else None,
                    "gender": person.gender if person else "",
                    "citizenship": person.citizenship if person else "",
                    "document_number": "X" * 9 if person and person.documents.exists() else "",
                    "document_expires": "2030-01-01"
                    if person and person.documents.filter(expires_at__isnull=False).exists()
                    else None,
                }
                errors = validate_row(row)
                assignment.validation_errors = errors
                assignment.status = "validated" if not errors else "assigned"
            elif action == "remove":
                assignment.status = "removed"
            assignment.save()
        return {"participant_id": str(participant.pk), "status": "ok"}


class GroupRequestsView(APIView):
    permission_classes = [require("orders.change")]

    def get(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        return Response([
            {"id": str(r.id), "subject": r.subject, "status": r.status,
             "sent_at": r.sent_at,
             "responses": [{"id": str(resp.id), "quoted_fare": str(resp.quoted_fare)
                            if resp.quoted_fare else None, "currency": resp.currency,
                            "received_at": resp.received_at}
                           for resp in r.responses.all()]}
            for r in group_order.requests.all()
        ])

    def post(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        group_request = GroupRequest.objects.create(
            tenant_id=group_order.tenant_id, group_order=group_order,
            subject=str(request.data.get("subject", "")),
            body=str(request.data.get("body", "")),
            status="sent", sent_at=timezone.now(), created_by=request.user,
        )
        return Response({"id": str(group_request.id)}, status=http.HTTP_201_CREATED)


class GroupSupplierResponsesView(APIView):
    permission_classes = [require("orders.change")]

    def post(self, request, group_order_id):
        group_order = _get_group_order(request, group_order_id)
        group_request = group_order.requests.filter(pk=request.data.get("request")).first()
        if group_request is None:
            raise ApiError(code="NOT_FOUND", message="Запрос не найден", status_code=404)
        response_obj = GroupSupplierResponse.objects.create(
            tenant_id=group_order.tenant_id, request=group_request,
            body=str(request.data.get("body", "")),
            quoted_fare=request.data.get("quoted_fare"),
            currency=str(request.data.get("currency", "")),
            conditions=request.data.get("conditions", {}),
            received_at=timezone.now(), created_by=request.user,
        )
        group_request.status = "answered"
        group_request.save(update_fields=["status"])
        return Response({"id": str(response_obj.id)}, status=http.HTTP_201_CREATED)


# --- Roster import (ТЗ §11) -----------------------------------------------------

class RosterImportCreateView(APIView):
    permission_classes = [require("orders.change")]

    def post(self, request):
        order = get_order_or_404(request.user, request.data.get("order"))
        file = request.FILES.get("file")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл file обязателен",
                           status_code=400)
        if file.size > 10 * 1024 * 1024:
            raise ApiError(code="FILE_TOO_LARGE", message="Максимум 10 МБ", status_code=400)
        import_job = RosterImportJob.objects.create(
            tenant_id=request.user.tenant_id, order=order, file_name=file.name,
            created_by=request.user,
        )
        try:
            raw_rows, parsed = parse_file(file.read(), file.name)
            import_job.raw_rows = raw_rows
            import_job.parsed_rows = parsed
            import_job.status = RosterImportJob.Status.PARSED
        except Exception as exc:  # noqa: BLE001
            import_job.status = RosterImportJob.Status.FAILED
            import_job.errors = [str(exc)[:500]]
        import_job.save()
        if import_job.status == RosterImportJob.Status.FAILED:
            raise ApiError(code="PARSE_FAILED", message="Не удалось разобрать файл",
                           details={"errors": import_job.errors}, status_code=422)
        return Response({"id": str(import_job.id), "rows": len(import_job.parsed_rows)},
                        status=http.HTTP_201_CREATED)


def _get_import(request, import_id) -> RosterImportJob:
    import_job = RosterImportJob.objects.filter(pk=import_id,
                                                tenant_id=request.user.tenant_id).first()
    if import_job is None:
        raise ApiError(code="NOT_FOUND", message="Импорт не найден", status_code=404)
    return import_job


class RosterImportPreviewView(APIView):
    permission_classes = [require("orders.change")]

    def post(self, request, import_id):
        import_job = _get_import(request, import_id)
        if import_job.status not in (RosterImportJob.Status.PARSED,
                                     RosterImportJob.Status.PREVIEW_READY):
            raise ApiError(code="INVALID_IMPORT_STATUS",
                           message=f"Preview недоступен в статусе {import_job.status}",
                           status_code=409)
        preview = build_preview(import_job.order, import_job.parsed_rows)
        import_job.preview = preview
        import_job.status = RosterImportJob.Status.PREVIEW_READY
        import_job.save(update_fields=["preview", "status"])
        return Response(preview)


class RosterImportApplyView(APIView):
    """Применяет решения keep_current/use_incoming/merge/add/ignore (ТЗ §11)."""

    permission_classes = [require("orders.change")]

    def post(self, request, import_id):
        import_job = _get_import(request, import_id)
        if import_job.status != RosterImportJob.Status.PREVIEW_READY:
            raise ApiError(code="PREVIEW_REQUIRED", message="Сначала выполните preview",
                           status_code=409)
        decisions = request.data.get("decisions", {})
        if not isinstance(decisions, dict):
            raise ApiError(code="VALIDATION_ERROR",
                           message="decisions: {row_index: decision}", status_code=400)
        results = []
        with transaction.atomic():
            for item in import_job.preview["items"]:
                index = item["row_index"]
                decision = decisions.get(str(index), "ignore")
                row = item["row"]
                result = {"row_index": index, "decision": decision, "status": "ok"}
                try:
                    self._apply_decision(import_job, item, decision, row, request)
                except ApiError as exc:
                    result.update(status="error", code=exc.code, message=exc.message)
                results.append(result)
            import_job.decisions = decisions
            import_job.status = RosterImportJob.Status.APPLIED
            import_job.applied_at = timezone.now()
            import_job.save(update_fields=["decisions", "status", "applied_at"])
        audit("groups.roster_applied", actor=request.user, resource=import_job.order,
              request=request, after={"decisions": len(decisions)})
        return Response({"results": results})

    def _apply_decision(self, import_job, item, decision, row, request) -> None:
        order = import_job.order
        if decision == "ignore" or decision == "keep_current":
            return
        if decision == "add" and item["state"] == "new":
            person = Person.objects.create(
                tenant_id=order.tenant_id,
                surname=row.get("surname") or row.get("latin_surname", ""),
                given_name=row.get("given_name") or row.get("latin_given_name", ""),
                latin_surname=row.get("latin_surname", ""),
                latin_given_name=row.get("latin_given_name", ""),
                birth_date=row.get("birth_date"),
                gender=row.get("gender", ""),
                citizenship=row.get("citizenship", ""),
                phone=row.get("phone", ""),
                email=row.get("email", ""),
                created_by=request.user,
            )
            OrderParticipant.objects.create(
                tenant_id=order.tenant_id, order=order, person=person,
                created_by=request.user,
            )
            RosterMergeHistory.objects.create(
                import_job=import_job, person=person, row_index=item["row_index"],
                decision=decision, after=row, applied_by=request.user,
            )
        elif decision in ("use_incoming", "merge") and item.get("participant_id"):
            participant = OrderParticipant.objects.filter(
                pk=item["participant_id"]).select_related("person").first()
            if participant is None or participant.person is None:
                raise ApiError(code="PARTICIPANT_NOT_FOUND", message="Участник не найден")
            person = participant.person
            before = {"phone": person.phone, "email": person.email,
                      "birth_date": str(person.birth_date) if person.birth_date else None,
                      "gender": person.gender, "citizenship": person.citizenship}
            for field in ("phone", "email", "gender", "citizenship"):
                incoming = row.get(field)
                if incoming and (decision == "use_incoming" or not getattr(person, field)):
                    setattr(person, field, incoming)
            if row.get("birth_date") and (decision == "use_incoming"
                                          or person.birth_date is None):
                person.birth_date = row["birth_date"]
            person.updated_by = request.user
            person.save()
            RosterMergeHistory.objects.create(
                import_job=import_job, person=person, row_index=item["row_index"],
                decision=decision, before=before, after=row, applied_by=request.user,
            )


class RosterImportExportView(APIView):
    """Экспорт списка в CSV c транслитерацией; исходные данные не меняются."""

    permission_classes = [require("orders.view")]

    def get(self, request, import_id):
        import_job = _get_import(request, import_id)
        export_format = request.query_params.get("format", "csv")
        if export_format != "csv":
            raise ApiError(code="UNSUPPORTED_FORMAT", message="Поддерживается format=csv",
                           status_code=400)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["SURNAME", "NAME", "BIRTH_DATE", "GENDER", "CITIZENSHIP",
                         "DOCUMENT"])
        for row in import_job.parsed_rows:
            surname = row.get("latin_surname") or transliterate(row.get("surname", ""))
            name = row.get("latin_given_name") or transliterate(row.get("given_name", ""))
            # защита от CSV formula injection (ТЗ §14.4)
            cells = [surname, name, row.get("birth_date") or "",
                     row.get("gender", ""), row.get("citizenship", ""),
                     row.get("document_number", "")]
            writer.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else c
                             for c in cells])
        response = HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="roster.csv"'
        return response
