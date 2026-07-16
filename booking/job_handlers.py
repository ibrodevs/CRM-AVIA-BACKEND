"""Saga бронирования и выписки (ТЗ §10). Не одна распределённая транзакция:
каждый внешний результат записывается; частичная ошибка не скрывает успехи.
"""
from django.db import transaction
from django.utils import timezone

from common.jobs import job_handler
from common.models import BackgroundJob
from common.outbox import emit_event
from integrations.adapters import (
    AdapterContext, AdapterError, AmbiguousResultError, get_adapter,
)


def _adapter_for(service):
    key = "mock"
    if service.supplier_id:
        credential = service.supplier.credentials.filter(
            archived_at__isnull=True, status="active"
        ).first()
        if credential is not None:
            key = credential.provider_adapter
    return get_adapter(key)


@job_handler("booking.run", user_cancellable=False)
def run_booking(job: BackgroundJob) -> dict:
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    ctx = AdapterContext(tenant_id=workflow.tenant_id,
                         correlation_id=job.correlation_id or str(job.id))
    booked = failed = 0

    for item in workflow.items.select_related("service").order_by("sequence"):
        if item.status != BookingWorkflowItem.Status.PENDING:
            continue
        service = item.service
        item.status = BookingWorkflowItem.Status.BOOKING
        item.save(update_fields=["status"])
        ctx.supplier_id = service.supplier_id
        try:
            adapter = _adapter_for(service)
            result = adapter.book(ctx, {
                "client_request_id": f"{workflow.id}:{item.id}",
                "service_kind": service.kind,
                "snapshot": service.provider_snapshot or {},
                "_mock": (service.provider_snapshot or {}).get("_mock", {}),
            })
        except AdapterError as exc:
            item.status = BookingWorkflowItem.Status.FAILED
            item.error_code = exc.code
            item.error_message = str(exc)
            item.save(update_fields=["status", "error_code", "error_message"])
            failed += 1
            _create_incident(workflow, service, exc, job)
            emit_event("booking.updated", workflow,
                       payload={"item": str(item.id), "status": "failed",
                                "error_code": exc.code})
            continue

        with transaction.atomic():
            item.status = BookingWorkflowItem.Status.BOOKED
            item.locator = result.get("locator", "")
            item.provider_result = result
            item.save(update_fields=["status", "locator", "provider_result"])
            service = OrderService.objects.select_for_update().get(pk=service.pk)
            service.status = OrderService.Status.BOOKED
            service.external_id = item.locator
            if deadline := result.get("ticketing_deadline"):
                service.ticketing_deadline = deadline
            service.version += 1
            service.save(update_fields=["status", "external_id", "ticketing_deadline",
                                        "version", "updated_at"])
            emit_event("booking.updated", workflow,
                       payload={"item": str(item.id), "status": "booked",
                                "locator": item.locator})
        booked += 1

    workflow.status = (BookingWorkflow.Status.COMPLETED if failed == 0 and booked
                       else BookingWorkflow.Status.PARTIAL if booked
                       else BookingWorkflow.Status.FAILED)
    workflow.save(update_fields=["status"])
    emit_event("booking.updated", workflow,
               payload={"status": workflow.status, "booked": booked, "failed": failed})
    return {"booked": booked, "failed": failed, "status": workflow.status}


@job_handler("booking.issue", user_cancellable=False)
def run_issue(job: BackgroundJob) -> dict:
    """Выписка. Ambiguous timeout переводит item в unknown и блокирует повтор
    до status inquiry (ТЗ §9.1, §30.1)."""
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    only_items = set(job.payload.get("item_ids") or [])
    ctx = AdapterContext(tenant_id=workflow.tenant_id,
                         correlation_id=job.correlation_id or str(job.id))
    issued = failed = unknown = 0

    for item in workflow.items.select_related("service").order_by("sequence"):
        if only_items and str(item.id) not in only_items:
            continue
        if item.status != BookingWorkflowItem.Status.BOOKED:
            continue
        service = item.service
        item.status = BookingWorkflowItem.Status.ISSUING
        item.save(update_fields=["status"])
        ctx.supplier_id = service.supplier_id
        try:
            adapter = _adapter_for(service)
            result = adapter.issue(ctx, {
                "locator": item.locator,
                "passengers": job.payload.get("passengers", []),
                "_mock": job.payload.get("_mock", {}),
            })
        except AmbiguousResultError:
            item.status = BookingWorkflowItem.Status.UNKNOWN
            item.error_code = "ISSUE_UNKNOWN"
            item.error_message = "Результат выписки неизвестен; требуется status inquiry"
            item.save(update_fields=["status", "error_code", "error_message"])
            unknown += 1
            _create_incident(workflow, service, AmbiguousResultError(), job,
                             severity="critical")
            emit_event("ticketing.updated", workflow,
                       payload={"item": str(item.id), "status": "unknown"})
            continue
        except AdapterError as exc:
            item.status = BookingWorkflowItem.Status.FAILED
            item.error_code = exc.code
            item.error_message = str(exc)
            item.save(update_fields=["status", "error_code", "error_message"])
            failed += 1
            _create_incident(workflow, service, exc, job)
            continue

        with transaction.atomic():
            item.status = BookingWorkflowItem.Status.ISSUED
            item.provider_result = result
            item.save(update_fields=["status", "provider_result"])
            service = OrderService.objects.select_for_update().get(pk=service.pk)
            service.status = OrderService.Status.ISSUED
            service.version += 1
            service.save(update_fields=["status", "version", "updated_at"])
            if service.kind == "avia":
                _save_tickets(workflow, service, item, result)
            emit_event("ticketing.updated", workflow,
                       payload={"item": str(item.id), "status": "issued"})
        issued += 1

    emit_event("ticketing.updated", workflow,
               payload={"issued": issued, "failed": failed, "unknown": unknown})
    return {"issued": issued, "failed": failed, "unknown": unknown}


def _save_tickets(workflow, service, item, result: dict) -> None:
    from avia.models import AviaBooking, Ticket

    booking, _ = AviaBooking.objects.get_or_create(
        tenant_id=workflow.tenant_id, client_request_id=f"{workflow.id}:{item.id}",
        defaults={"service": service, "provider_adapter": "mock",
                  "locator": item.locator, "status": AviaBooking.Status.TICKETED},
    )
    for ticket in result.get("tickets", []):
        number = ticket.get("ticket_number", "")
        if number and not Ticket.objects.filter(validating_carrier="XX",
                                                ticket_number=number).exists():
            Ticket.objects.create(
                tenant_id=workflow.tenant_id, booking=booking,
                validating_carrier="XX", ticket_number=number,
                issued_at=timezone.now(),
            )


@job_handler("booking.status_inquiry", user_cancellable=False)
def status_inquiry(job: BackgroundJob) -> dict:
    """Retrieve после unknown: без inquiry повтор выписки заблокирован."""
    from booking.models import BookingWorkflowItem
    from services.models import OrderService

    item = BookingWorkflowItem.objects.select_related("service", "workflow").get(
        pk=job.payload["item_id"]
    )
    ctx = AdapterContext(tenant_id=item.tenant_id, supplier_id=item.service.supplier_id,
                         correlation_id=job.correlation_id or str(job.id))
    adapter = _adapter_for(item.service)
    try:
        result = adapter.retrieve_booking(ctx, item.locator)
    except AdapterError as exc:
        return {"status": "inquiry_failed", "error_code": exc.code}

    provider_status = result.get("status")
    if provider_status == "issued":
        item.status = BookingWorkflowItem.Status.ISSUED
        item.provider_result = result
        item.save(update_fields=["status", "provider_result"])
        service = item.service
        service.status = OrderService.Status.ISSUED
        service.version += 1
        service.save(update_fields=["status", "version", "updated_at"])
        _save_tickets(item.workflow, service, item, result)
    elif provider_status in ("booked", "cancelled"):
        item.status = (BookingWorkflowItem.Status.BOOKED if provider_status == "booked"
                       else BookingWorkflowItem.Status.COMPENSATED)
        item.save(update_fields=["status"])
    emit_event("ticketing.updated", item.workflow,
               payload={"item": str(item.id), "status": item.status,
                        "inquiry_result": provider_status})
    return {"status": item.status, "provider_status": provider_status}


@job_handler("booking.compensate", user_cancellable=False)
def compensate(job: BackgroundJob) -> dict:
    """Compensating cancellation успешных броней по запросу оператора (ТЗ §10)."""
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    ctx = AdapterContext(tenant_id=workflow.tenant_id,
                         correlation_id=job.correlation_id or str(job.id))
    compensated = 0
    for item in workflow.items.filter(status=BookingWorkflowItem.Status.BOOKED):
        ctx.supplier_id = item.service.supplier_id
        try:
            adapter = _adapter_for(item.service)
            adapter.cancel(ctx, item.locator)
        except AdapterError as exc:
            _create_incident(workflow, item.service, exc, job)
            continue
        item.status = BookingWorkflowItem.Status.COMPENSATED
        item.save(update_fields=["status"])
        service = item.service
        service.status = OrderService.Status.CANCELLED
        service.version += 1
        service.save(update_fields=["status", "version", "updated_at"])
        compensated += 1
    workflow.status = BookingWorkflow.Status.CANCELLED
    workflow.save(update_fields=["status"])
    emit_event("booking.updated", workflow, payload={"status": "cancelled",
                                                     "compensated": compensated})
    return {"compensated": compensated}


def _create_incident(workflow, service, exc, job, *, severity: str = "high") -> None:
    from integrations.models import IntegrationIncident

    IntegrationIncident.objects.create(
        tenant_id=workflow.tenant_id,
        error_code=getattr(exc, "code", "UNKNOWN"),
        severity=severity,
        operation=job.kind,
        supplier=service.supplier,
        order=workflow.order,
        service=service,
        job=job,
        sanitized_error=str(exc)[:2000],
        correlation_id=job.correlation_id,
    )
