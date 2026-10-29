from django.db import transaction
from django.utils import timezone

from booking.contacts import booking_contact_error
from common.jobs import job_handler
from common.models import BackgroundJob
from common.outbox import emit_event
from integrations.adapters import (
    AdapterContext,
    AdapterError,
    AmbiguousResultError,
    adapter_for_service,
)
from orders.contacts import booking_contact_info


def _stored_locator(service):
    snapshot = service.provider_snapshot or {}
    return snapshot.get(snapshot.get("provider_adapter", "") + "_booking", {}).get("locator", "")


def _adapter_for(service):
    return adapter_for_service(service)


@job_handler("booking.run", user_cancellable=False)
def run_booking(job: BackgroundJob) -> dict:
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    ctx = AdapterContext(tenant_id=workflow.tenant_id, correlation_id=job.correlation_id or str(job.id))
    booked = failed = pending = 0

    for item in workflow.items.select_related("service").order_by("sequence"):
        if item.status == BookingWorkflowItem.Status.BOOKING and (item.service.provider_snapshot or {}).get(
            "provider_adapter"
        ) not in (None, "mock"):
            item.status = BookingWorkflowItem.Status.UNKNOWN
            item.locator = _stored_locator(item.service)
            item.error_code = "BOOKING_UNKNOWN"
            item.save(update_fields=["status", "locator", "error_code"])
        if item.status == BookingWorkflowItem.Status.UNKNOWN:
            pending += 1
            continue
        if item.status == BookingWorkflowItem.Status.BOOKED:
            booked += 1
            continue
        if item.status != BookingWorkflowItem.Status.PENDING:
            continue
        service = item.service
        item.status = BookingWorkflowItem.Status.BOOKING
        item.save(update_fields=["status"])
        ctx.supplier_id = service.supplier_id
        ctx.order_id = service.order_id
        try:
            adapter = _adapter_for(service)
            contact_error = booking_contact_error(workflow.order, [service])
            if contact_error:
                raise AdapterError(contact_error["code"], contact_error["message"], category="validation")
            result = adapter.book(
                ctx,
                {
                    "client_request_id": f"{workflow.id}:{item.id}",
                    "service_id": str(service.id),
                    "passengers": _booking_passengers(service),
                    "contact_info": _booking_contact(workflow.order),
                    "customer": _booking_customer(workflow.order),
                    "service_kind": service.kind,
                    "snapshot": service.provider_snapshot or {},
                    "_mock": (service.provider_snapshot or {}).get("_mock", {}),
                },
            )
            if result.get("status") in ("failed", "cancelled"):
                # Success Id acknowledges creation; the supplier can still refuse.
                with transaction.atomic():
                    service = OrderService.objects.select_for_update().get(pk=service.pk)
                    service.status = (
                        OrderService.Status.FAILED
                        if result["status"] == "failed"
                        else OrderService.Status.CANCELLED
                    )
                    if result.get("provider_snapshot") and not adapter.persists_booking_snapshot:
                        service.provider_snapshot = result["provider_snapshot"]
                    service.external_id = result.get("locator", "")
                    service.version += 1
                    service.save(
                        update_fields=["status", "provider_snapshot", "external_id", "version", "updated_at"]
                    )
                raise AdapterError(
                    "BOOKING_REJECTED",
                    "Поставщик отказал в бронировании или отменил заказ",
                    category="booking",
                )
        except AdapterError as exc:
            if exc.category == "booking_unknown":
                service.refresh_from_db()
                item.status = BookingWorkflowItem.Status.UNKNOWN
                item.locator = _stored_locator(service)
                item.error_code = exc.code
                item.error_message = str(exc)
                item.save(update_fields=["status", "locator", "error_code", "error_message"])
                pending += 1
                _create_incident(workflow, service, exc, job)
                continue
            item.status = BookingWorkflowItem.Status.FAILED
            item.error_code = exc.code
            item.error_message = str(exc)
            item.save(update_fields=["status", "error_code", "error_message"])
            failed += 1
            _create_incident(workflow, service, exc, job)
            emit_event(
                "booking.updated",
                workflow,
                payload={"item": str(item.id), "status": "failed", "error_code": exc.code},
            )
            continue

        with transaction.atomic():
            is_pending = result.get("status") == "pending"
            item.status = (
                BookingWorkflowItem.Status.UNKNOWN if is_pending else BookingWorkflowItem.Status.BOOKED
            )
            item.locator = result.get("locator", "")
            item.provider_result = result
            item.save(update_fields=["status", "locator", "provider_result"])
            service = OrderService.objects.select_for_update().get(pk=service.pk)
            service.status = OrderService.Status.APPROVAL if is_pending else OrderService.Status.BOOKED
            if result.get("provider_snapshot") and not adapter.persists_booking_snapshot:
                service.provider_snapshot = result["provider_snapshot"]
            service.external_id = item.locator
            if deadline := result.get("ticketing_deadline"):
                service.ticketing_deadline = deadline
            service.version += 1
            service.save(
                update_fields=[
                    "status",
                    "provider_snapshot",
                    "external_id",
                    "ticketing_deadline",
                    "version",
                    "updated_at",
                ]
            )
            emit_event(
                "booking.updated",
                workflow,
                payload={"item": str(item.id), "status": item.status, "locator": item.locator},
            )
        pending += int(is_pending)
        booked += int(not is_pending)

    workflow.status = (
        BookingWorkflow.Status.COMPLETED
        if failed == 0 and booked and not pending
        else BookingWorkflow.Status.PARTIAL
        if booked or pending
        else BookingWorkflow.Status.FAILED
    )
    workflow.save(update_fields=["status"])
    emit_event(
        "booking.updated", workflow, payload={"status": workflow.status, "booked": booked, "failed": failed}
    )
    return {"booked": booked, "failed": failed, "pending": pending, "status": workflow.status}


@job_handler("booking.issue", user_cancellable=False)
def run_issue(job: BackgroundJob) -> dict:
    """Выписка. Ambiguous timeout переводит item в unknown и блокирует повтор
    до status inquiry (ТЗ §9.1, §30.1)."""
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    only_items = set(job.payload.get("item_ids") or [])
    ctx = AdapterContext(tenant_id=workflow.tenant_id, correlation_id=job.correlation_id or str(job.id))
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
            result = adapter.issue(
                ctx,
                {
                    "locator": item.locator,
                    "passengers": job.payload.get("passengers", []),
                    "_mock": job.payload.get("_mock", {}),
                },
            )
        except AmbiguousResultError:
            item.status = BookingWorkflowItem.Status.UNKNOWN
            item.error_code = "ISSUE_UNKNOWN"
            item.error_message = "Результат выписки неизвестен; требуется status inquiry"
            item.save(update_fields=["status", "error_code", "error_message"])
            unknown += 1
            _create_incident(workflow, service, AmbiguousResultError(), job, severity="critical")
            emit_event("ticketing.updated", workflow, payload={"item": str(item.id), "status": "unknown"})
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
            emit_event("ticketing.updated", workflow, payload={"item": str(item.id), "status": "issued"})
        issued += 1

    emit_event(
        "ticketing.updated", workflow, payload={"issued": issued, "failed": failed, "unknown": unknown}
    )
    return {"issued": issued, "failed": failed, "unknown": unknown}


def _save_tickets(workflow, service, item, result: dict) -> None:
    from avia.models import AviaBooking, Ticket

    booking, _ = AviaBooking.objects.get_or_create(
        tenant_id=workflow.tenant_id,
        client_request_id=f"{workflow.id}:{item.id}",
        defaults={
            "service": service,
            "provider_adapter": "mock",
            "locator": item.locator,
            "status": AviaBooking.Status.TICKETED,
        },
    )
    for ticket in result.get("tickets", []):
        number = ticket.get("ticket_number", "")
        if number and not Ticket.objects.filter(validating_carrier="XX", ticket_number=number).exists():
            Ticket.objects.create(
                tenant_id=workflow.tenant_id,
                booking=booking,
                validating_carrier="XX",
                ticket_number=number,
                issued_at=timezone.now(),
            )


@job_handler("booking.status_inquiry", user_cancellable=False)
def status_inquiry(job: BackgroundJob) -> dict:
    """Retrieve после unknown: без inquiry повтор выписки заблокирован."""
    from booking.models import BookingWorkflowItem
    from services.models import OrderService

    item = BookingWorkflowItem.objects.select_related("service", "workflow").get(pk=job.payload["item_id"])
    ctx = AdapterContext(
        tenant_id=item.tenant_id,
        supplier_id=item.service.supplier_id,
        correlation_id=job.correlation_id or str(job.id),
    )
    try:
        adapter = _adapter_for(item.service)
        result = adapter.retrieve_booking(ctx, item.locator)
    except AdapterError as exc:
        return {"status": "inquiry_failed", "error_code": exc.code}

    with transaction.atomic():
        service = OrderService.objects.select_for_update().get(pk=item.service_id)
        item.refresh_from_db()
        item.service = service
        key = (service.provider_snapshot or {}).get("provider_adapter", "") + "_booking"
        if (
            adapter.persists_booking_snapshot
            and result.get("provider_snapshot")
            and (service.provider_snapshot or {}).get(key) != result["provider_snapshot"].get(key)
        ):
            # A later supplier operation has already committed its reservation
            # or result. Applying this older inquiry must not overwrite it.
            return {"status": item.status, "provider_status": result.get("status"), "stale": True}
        provider_status = result.get("status")
        if provider_status == "failed":
            item.provider_result = result
            item.status = BookingWorkflowItem.Status.FAILED
            item.error_code = "BOOKING_REJECTED"
            item.error_message = "Поставщик отказал в бронировании"
            item.save(update_fields=["status", "provider_result", "error_code", "error_message"])
            service = item.service
            service.status = OrderService.Status.FAILED
            if result.get("provider_snapshot") and not adapter.persists_booking_snapshot:
                service.provider_snapshot = result["provider_snapshot"]
            service.version += 1
            service.save(
                update_fields=["status", "version", "updated_at"]
                + ([] if adapter.persists_booking_snapshot else ["provider_snapshot"])
            )
            item.workflow.status = (
                "partial"
                if item.workflow.items.exclude(status__in=["failed", "skipped"]).exists()
                else "failed"
            )
            item.workflow.save(update_fields=["status"])
            _create_incident(
                item.workflow,
                service,
                AdapterError("BOOKING_REJECTED", "Поставщик отказал в бронировании", category="booking"),
                job,
            )
        if provider_status == "pending":
            item.provider_result = result
            item.status = BookingWorkflowItem.Status.UNKNOWN
            item.save(update_fields=["status", "provider_result"])
            fields = {"status": OrderService.Status.APPROVAL}
            if result.get("provider_snapshot") and not adapter.persists_booking_snapshot:
                fields["provider_snapshot"] = result["provider_snapshot"]
            OrderService.objects.filter(pk=item.service_id).update(**fields)
            item.workflow.status = "partial"
            item.workflow.save(update_fields=["status"])

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
            item.provider_result = result
            service = item.service
            service.status = (
                OrderService.Status.BOOKED if provider_status == "booked" else OrderService.Status.CANCELLED
            )
            if result.get("provider_snapshot") and not adapter.persists_booking_snapshot:
                service.provider_snapshot = result["provider_snapshot"]
            service.external_id = item.locator
            service.version += 1
            service.save(
                update_fields=["status", "external_id", "version", "updated_at"]
                + ([] if adapter.persists_booking_snapshot else ["provider_snapshot"])
            )
            item.status = (
                BookingWorkflowItem.Status.BOOKED
                if provider_status == "booked"
                else BookingWorkflowItem.Status.COMPENSATED
            )
            item.error_code = ""
            item.error_message = ""
            item.save(update_fields=["status", "provider_result", "error_code", "error_message"])
            if not item.workflow.items.exclude(status="booked").exists():
                item.workflow.status = "completed"
                item.workflow.save(update_fields=["status"])
            elif not item.workflow.items.exclude(status__in=["compensated", "failed", "skipped"]).exists():
                item.workflow.status = "cancelled"
                item.workflow.save(update_fields=["status"])
        emit_event(
            "ticketing.updated",
            item.workflow,
            payload={"item": str(item.id), "status": item.status, "inquiry_result": provider_status},
        )
        return {"status": item.status, "provider_status": provider_status}


@job_handler("booking.compensate", user_cancellable=False)
def compensate(job: BackgroundJob) -> dict:
    """Compensating cancellation успешных броней по запросу оператора (ТЗ §10)."""
    from booking.models import BookingWorkflow, BookingWorkflowItem
    from services.models import OrderService

    workflow = BookingWorkflow.objects.get(pk=job.payload["workflow_id"])
    ctx = AdapterContext(tenant_id=workflow.tenant_id, correlation_id=job.correlation_id or str(job.id))
    compensated = 0
    items = workflow.items.filter(status=BookingWorkflowItem.Status.BOOKED)
    if job.payload.get("service_ids"):
        items = items.filter(service_id__in=job.payload["service_ids"])
    for item in items:
        ctx.supplier_id = item.service.supplier_id
        try:
            adapter = _adapter_for(item.service)
            result = adapter.cancel(ctx, item.locator)
            if result.get("status") != "cancelled":
                raise AdapterError("CANCEL_PENDING", "Отмена ещё не подтверждена", category="sync")
        except AdapterError as exc:
            _create_incident(workflow, item.service, exc, job)
            continue
        item.status = BookingWorkflowItem.Status.COMPENSATED
        item.provider_result = result
        item.save(update_fields=["status", "provider_result"])
        service = item.service
        service.status = OrderService.Status.CANCELLED
        service.version += 1
        service.save(update_fields=["status", "version", "updated_at"])
        compensated += 1
    remaining = workflow.items.exclude(status__in=["compensated", "failed", "skipped"]).exists()
    workflow.status = BookingWorkflow.Status.PARTIAL if remaining else BookingWorkflow.Status.CANCELLED
    workflow.save(update_fields=["status"])
    emit_event("booking.updated", workflow, payload={"status": workflow.status, "compensated": compensated})
    return {"compensated": compensated}


def _create_incident(workflow, service, exc, job, *, severity: str = "high") -> None:
    from integrations.models import IntegrationIncident

    IntegrationIncident.objects.create(
        tenant_id=workflow.tenant_id,
        error_code=getattr(exc, "code", "UNKNOWN"),
        severity=severity,
        operation=job.kind,
        provider_adapter=(service.provider_snapshot or {}).get("provider_adapter", ""),
        supplier=service.supplier,
        order=workflow.order,
        service=service,
        job=job,
        sanitized_error=str(exc)[:2000],
        correlation_id=job.correlation_id,
    )


def _booking_passengers(service):
    passengers = []
    for row in service.passengers.filter(status="active", participant__status="active").select_related(
        "participant__person"
    ):
        person = row.participant.person
        if person:
            data = {
                name: str(getattr(person, name) or "")
                for name in (
                    "latin_given_name",
                    "latin_surname",
                    "given_name",
                    "surname",
                    "birth_date",
                    "gender",
                    "citizenship",
                )
            }
        else:
            data = dict(row.participant.guest_snapshot or {})
        data["room_ref"] = row.room_ref
        data["is_main"] = str(row.participant_id) == (service.provider_snapshot or {}).get("main_guest")
        passengers.append(data)
    return sorted(passengers, key=lambda p: not p.get("is_main", False))


def _booking_contact(order):
    return booking_contact_info(order)


def _booking_customer(order):
    if order.client_company:
        company = order.client_company
        return {
            "type": "LEGAL",
            "name": company.legal_name,
            "inn": company.tax_id,
            "address": company.legal_address,
        }
    if order.client_person:
        return {"type": "PRIVATE"}
    return None
