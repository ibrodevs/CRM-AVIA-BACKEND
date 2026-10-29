"""Durable hotel operations; cancel is submitted at most once by this workflow."""

from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from aftersales.models import HotelOperation, HotelOperationHistory
from common.audit import audit
from common.errors import ApiError
from common.jobs import enqueue
from common.logging import redact
from common.outbox import emit_event
from integrations.adapters import AdapterContext, AdapterError, adapter_for_service
from integrations.hotelbook_observability import sanitize
from services.hotel_actions.presentation import booking_summary, capabilities, permissions
from services.models import OrderService

ACTIVE_CANCEL = ("submitted_to_supplier", "awaiting_supplier", "unknown", "checking")


def record(operation, action, before=None, actor=None):
    after = {
        "status": operation.status,
        "booking_reference": operation.supplier_reference,
        "room_refs": operation.room_refs,
        "result": operation.result_snapshot,
        "financial": operation.financial_snapshot,
        "requested_changes": operation.requested_changes,
        "supplier_response": operation.supplier_response,
    }
    HotelOperationHistory.objects.create(
        operation=operation,
        actor=actor or operation.actor,
        action=action,
        before=redact(before or {}),
        after=redact(after),
    )
    audit(
        f"hotel.{action}",
        actor=actor or operation.actor,
        resource=operation,
        tenant_id=operation.tenant_id,
        before=before,
        after=after,
    )
    emit_event(
        "service.updated",
        operation.service,
        payload={"hotel_operation": str(operation.id), "operation_status": operation.status},
    )


def require_permission(user, name):
    if not permissions(user).get(name):
        raise ApiError(
            code="PERMISSION_DENIED", message="Нет права на это действие гостиничной услуги", status_code=403
        )


def create_operation(service, user, data, key):
    if not key or len(key) > 128:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="Укажите Idempotency-Key (до 128 символов)",
            status_code=400,
        )
    action, scope = data["operation_type"], data["scope"]
    require_permission(
        user,
        "cancel"
        if action in ("cancel_booking", "cancel_rooms", "request_free_cancel")
        else "modify"
        if action != "refresh"
        else "view",
    )
    with transaction.atomic():
        service = OrderService.objects.select_for_update().get(pk=service.pk)
        old = service.hotel_operations.filter(idempotency_key=key).first()
        changes = redact(data.get("requested_changes", {}))
        refs = sorted(set(data.get("room_refs", [])))
        if old:
            if (
                old.operation_type != action
                or old.scope != scope
                or sorted(old.room_refs) != refs
                or old.requested_changes != changes
            ):
                raise ApiError(
                    code="IDEMPOTENCY_CONFLICT",
                    message="Ключ уже использован для другого запроса",
                    status_code=409,
                )
            return old
        cap = capabilities(service).get(action)
        if not cap or cap["mode"] in ("unsupported", "new_booking", "local"):
            raise ApiError(
                code="HOTEL_ACTION_UNSUPPORTED",
                message=(cap or {}).get("reason", "Действие недоступно"),
                status_code=409,
            )
        summary = booking_summary(service)
        available = {r["ref"] for r in summary["rooms"]}
        if not set(refs).issubset(available):
            raise ApiError(
                code="INVALID_ROOM_REFS", message="Номера не принадлежат этой брони", status_code=400
            )
        if action == "cancel_booking" and (scope != "booking" or set(refs) != available):
            raise ApiError(
                code="WHOLE_BOOKING_REQUIRED",
                message="Поставщик поддерживает только отмену всей брони. Выберите все номера.",
                status_code=409,
            )
        if action == "refresh" and scope != "booking":
            raise ApiError(
                code="WHOLE_BOOKING_REQUIRED", message="Проверяется статус всей брони", status_code=400
            )
        if scope == "rooms" and not refs:
            raise ApiError(code="ROOMS_REQUIRED", message="Выберите номера", status_code=400)
        if (
            action == "cancel_booking"
            and service.hotel_operations.filter(
                operation_type="cancel_booking", status__in=ACTIVE_CANCEL
            ).exists()
        ):
            raise ApiError(
                code="CANCEL_REQUIRES_CHECK",
                message="Отмена уже отправлена. Выполните проверку статуса; повторная отправка запрещена.",
                status_code=409,
            )
        if cap["mode"] == "manual" and not str(changes.get("description", "")).strip():
            raise ApiError(code="DESCRIPTION_REQUIRED", message="Опишите запрос оператору", status_code=400)
        operation = HotelOperation.objects.create(
            tenant_id=service.tenant_id,
            order=service.order,
            service=service,
            supplier=service.supplier,
            operation_type=action,
            scope=scope,
            room_refs=refs,
            execution_mode=cap["mode"],
            original_snapshot=summary,
            requested_changes=changes,
            financial_snapshot=summary["financial"],
            supplier_reference=summary["supplier_booking_reference"],
            actor=user,
            created_by=user,
            idempotency_key=key,
            status="awaiting_operator_confirmation"
            if action == "cancel_booking" and cap["mode"] == "api"
            else "created",
        )
        record(operation, "created", actor=user)
        if action == "refresh" and cap["mode"] == "api":
            submit(operation, user)
        return operation


def submit(operation, user):
    operation.status = "submitted_to_supplier"
    operation.version += 1
    operation.save(update_fields=["status", "version", "updated_at"])
    job = enqueue(
        "hotel.operation",
        {"operation_id": str(operation.id)},
        initiated_by=user,
        tenant_id=operation.tenant_id,
    )
    operation.job = job
    operation.save(update_fields=["job", "updated_at"])
    record(operation, "submitted", actor=user)


def confirm_operation(service, operation_id, user, data):
    require_permission(user, "cancel")
    require_permission(user, "financial_confirmation")
    with transaction.atomic():
        service = OrderService.objects.select_for_update().get(pk=service.pk)
        operation = service.hotel_operations.select_for_update().filter(pk=operation_id).first()
        if not operation:
            raise ApiError(code="NOT_FOUND", message="Операция не найдена", status_code=404)
        if operation.operation_type != "cancel_booking" or operation.execution_mode != "api":
            raise ApiError(
                code="INVALID_OPERATION", message="Эта операция не выполняет API-отмену", status_code=409
            )
        if operation.status != "awaiting_operator_confirmation":
            # Retransmission of the confirmation never queues another cancel.
            return operation
        if data.get("confirmed") is not True or data.get("accept_unknown_penalty") is not True:
            raise ApiError(
                code="CONFIRMATION_REQUIRED",
                message="Подтвердите отмену всей брони и финансовые условия с неизвестным точным штрафом",
                status_code=400,
            )
        from common.locking import check_version

        check_version(operation, data.get("version"))
        if (
            capabilities(service)["cancel_booking"]["mode"] != "api"
            or booking_summary(service) != operation.original_snapshot
        ):
            raise ApiError(
                code="BOOKING_CHANGED",
                message="Данные брони изменились. Обновите условия и создайте новое подтверждение.",
                status_code=409,
            )
        if (
            service.hotel_operations.exclude(pk=operation.pk)
            .filter(operation_type="cancel_booking", status__in=ACTIVE_CANCEL)
            .exists()
        ):
            raise ApiError(
                code="CANCEL_REQUIRES_CHECK",
                message="Отмена уже выполняется. Проверьте статус.",
                status_code=409,
            )
        operation.confirmed_at = timezone.now()
        operation.financial_snapshot["confirmation"] = {
            "actor": str(user.pk),
            "at": operation.confirmed_at.isoformat(),
            "accepted_unknown_penalty": True,
        }
        operation.save(update_fields=["confirmed_at", "financial_snapshot"])
        record(operation, "financial_confirmation", actor=user)
        submit(operation, user)
        return operation


def _apply_supplier_result(service, result):
    """Only a confirmed supplier outcome changes CRM booking status. Never rewrite costs."""
    from booking.models import BookingWorkflowItem
    from integrations.adapters import adapter_for_service

    with transaction.atomic():
        service = OrderService.objects.select_for_update().get(pk=service.pk)
        status = result.get("status")
        if status == "cancelled":
            service.status = "cancelled"
            service.workflow_items.filter(status__in=["booked", "unknown", "booking"]).update(
                status=BookingWorkflowItem.Status.COMPENSATED, provider_result=sanitize(result)
            )
        elif status == "booked" and service.status not in ("cancelled", "refunded", "issued"):
            service.status = "booked"
            service.workflow_items.filter(status__in=["booked", "unknown", "booking"]).update(
                status=BookingWorkflowItem.Status.BOOKED,
                provider_result=sanitize(result),
                error_code="",
                error_message="",
            )
        if status in ("booked", "cancelled") and result.get("locator"):
            service.external_id = result["locator"]
        if result.get("provider_snapshot") and not adapter_for_service(service).persists_booking_snapshot:
            service.provider_snapshot = deepcopy(result["provider_snapshot"])
        service.version += 1
        service.save(update_fields=["status", "external_id", "provider_snapshot", "version", "updated_at"])
        if status == "cancelled":
            for workflow in service.order.booking_workflows.filter(items__service=service):
                if not workflow.items.exclude(status__in=["compensated", "failed", "skipped"]).exists():
                    workflow.status = "cancelled"
                    workflow.save(update_fields=["status", "updated_at"])
        elif status == "booked":
            for workflow in service.order.booking_workflows.filter(items__service=service):
                if not workflow.items.exclude(status="booked").exists():
                    workflow.status = "completed"
                    workflow.save(update_fields=["status", "updated_at"])
        return service


def execute_operation(job):
    # A worker crash after dispatch leaves awaiting_supplier. A redelivered job
    # cannot call cancel again; a refresh operation is the only recovery path.
    with transaction.atomic():
        operation = HotelOperation.objects.select_for_update().get(
            pk=job.payload["operation_id"], tenant_id=job.tenant_id
        )
        if operation.status != "submitted_to_supplier":
            return {"operation_status": operation.status}
        operation.status = "awaiting_supplier"
        operation.save(update_fields=["status", "updated_at"])
        record(operation, "dispatch")
    service = operation.service
    try:
        adapter = adapter_for_service(service)
        locator = operation.original_snapshot.get("locator")
        if not locator:
            raise AdapterError("BOOKING_NOT_FOUND", "Нет номера брони", category="validation")
        if (
            operation.operation_type == "cancel_booking"
            and booking_summary(service) != operation.original_snapshot
        ):
            raise AdapterError("BOOKING_CHANGED", "Данные брони изменились", category="validation")
        ctx = AdapterContext(
            tenant_id=service.tenant_id, supplier_id=service.supplier_id, correlation_id=str(job.id)
        )
        ctx.order_id = service.order_id
        if operation.operation_type == "cancel_booking":
            require_permission(operation.actor, "cancel")
            require_permission(operation.actor, "financial_confirmation")
        result = (
            adapter.cancel(ctx, locator)
            if operation.operation_type == "cancel_booking"
            else adapter.retrieve_booking(ctx, locator)
        )
        service = _apply_supplier_result(service, result)
        operation.supplier_response = sanitize(result)
        status = result.get("status")
        operation.status = (
            "completed"
            if operation.operation_type == "refresh"
            and status in ("booked", "issued", "cancelled")
            or operation.operation_type == "cancel_booking"
            and status == "cancelled"
            else "unknown"
        )
    except ApiError:
        operation.status = "rejected"
        operation.supplier_response = {"error_code": "PERMISSION_DENIED"}
    except AdapterError as exc:
        # Do not persist raw exception text: supplier messages can echo credentials.
        operation.supplier_response = {"error_code": exc.code}
        operation.status = (
            "rejected"
            if exc.code in ("CANCEL_NOT_ALLOWED", "CANCELLATION_FAILED")
            else "failed"
            if exc.category in ("validation", "configuration")
            else "unknown"
        )
        if operation.operation_type == "cancel_booking" and operation.status == "unknown":
            try:
                # Status inquiry is read-only and safe. Never retry cancel here.
                result = adapter.retrieve_booking(ctx, operation.original_snapshot["locator"])
                service = _apply_supplier_result(service, result)
                operation.supplier_response["status_inquiry"] = sanitize(result)
                if result.get("status") == "cancelled":
                    operation.status = "completed"
            except Exception:
                pass
    except Exception:
        operation.status = "unknown"
        operation.supplier_response = {"error_code": "REQUIRES_STATUS_INQUIRY"}
    service.refresh_from_db()
    operation.result_snapshot = booking_summary(service)
    operation.completed_at = (
        timezone.now() if operation.status in ("completed", "failed", "rejected") else None
    )
    with transaction.atomic():
        current = HotelOperation.objects.select_for_update().get(pk=operation.pk)
        # A simultaneous status inquiry may already have confirmed cancellation.
        # A late timeout result must never undo that confirmed outcome.
        if current.status == "completed":
            operation.status = current.status
            operation.result_snapshot = current.result_snapshot
            operation.completed_at = current.completed_at
        operation.version = current.version + 1
        operation.save(
            update_fields=[
                "status",
                "supplier_response",
                "result_snapshot",
                "completed_at",
                "version",
                "updated_at",
            ]
        )
        record(operation, "result", before=operation.original_snapshot)
    if operation.operation_type == "refresh" and operation.result_snapshot["service_status"] == "cancelled":
        with transaction.atomic():
            for pending in service.hotel_operations.select_for_update().filter(
                operation_type="cancel_booking", status__in=ACTIVE_CANCEL
            ):
                pending.status, pending.result_snapshot, pending.completed_at = (
                    "completed",
                    operation.result_snapshot,
                    timezone.now(),
                )
                pending.version += 1
                pending.save(
                    update_fields=["status", "result_snapshot", "completed_at", "version", "updated_at"]
                )
                record(pending, "status_reconciled", actor=operation.actor)
    return {"operation_status": operation.status}
