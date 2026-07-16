"""Preflight-проверки перед бронированием (ТЗ §10)."""
from datetime import date

from django.utils import timezone


def run_preflight(workflow, user) -> dict:
    """Возвращает ok/warnings/blocking_errors/required_approvals/price_changes."""
    from accounts.permissions import has_permission, has_service_action

    warnings: list[dict] = []
    blocking: list[dict] = []
    approvals: list[dict] = []
    price_changes: list[dict] = []

    order = workflow.order
    items = list(workflow.items.select_related("service"))
    if not items:
        blocking.append({"code": "NO_SERVICES", "message": "В workflow нет услуг"})

    # права оператора по каждой услуге
    for item in items:
        service = item.service
        if not has_permission(user, "services.book"):
            blocking.append({"code": "PERMISSION_DENIED",
                             "message": "Нет права services.book"})
            break
        if not has_service_action(user, service.kind, "book"):
            blocking.append({"code": "SERVICE_KIND_FORBIDDEN",
                             "message": f"Нет доступа к бронированию {service.kind}",
                             "service_id": str(service.id)})

    # отсутствие конфликтующих активных операций
    from booking.models import BookingWorkflow

    conflicting = BookingWorkflow.objects.filter(
        order=order, status=BookingWorkflow.Status.RUNNING
    ).exclude(pk=workflow.pk)
    if conflicting.exists():
        blocking.append({"code": "CONCURRENT_WORKFLOW",
                         "message": "По заказу уже выполняется бронирование"})

    # документы и данные участников (латиница, ДР, пол, гражданство, срок действия)
    participants = order.participants.filter(status="active").select_related("person")
    for participant in participants:
        person = participant.person
        if person is None:
            continue
        missing = []
        if not person.latin_surname or not person.latin_given_name:
            missing.append("латинские ФИО")
        if not person.birth_date:
            missing.append("дата рождения")
        if not person.gender:
            missing.append("пол")
        if not person.citizenship:
            missing.append("гражданство")
        if missing:
            blocking.append({
                "code": "PASSENGER_DATA_MISSING",
                "message": f"{person.full_name}: не заполнены {', '.join(missing)}",
                "person_id": str(person.id),
            })
        document = participant.booking_document
        if document is None:
            document = person.documents.filter(archived_at__isnull=True).first()
        if document is None:
            blocking.append({"code": "DOCUMENT_MISSING",
                             "message": f"{person.full_name}: нет документа",
                             "person_id": str(person.id)})
        elif document.expires_at:
            trip_end = order.planned_end or order.planned_start or date.today()
            if document.expires_at < trip_end:
                blocking.append({"code": "DOCUMENT_EXPIRED",
                                 "message": f"{person.full_name}: документ истекает "
                                            f"{document.expires_at}",
                                 "person_id": str(person.id)})
            elif (document.expires_at - trip_end).days < 180:
                warnings.append({"code": "DOCUMENT_EXPIRES_SOON",
                                 "message": f"{person.full_name}: до конца действия документа "
                                            f"менее 6 месяцев",
                                 "person_id": str(person.id)})

    # availability и повторная цена (revalidate через адаптер)
    from integrations.adapters import AdapterContext, AdapterError, get_adapter

    for item in items:
        service = item.service
        snapshot = service.provider_snapshot or {}
        if service.source != "api" or not snapshot:
            continue
        try:
            adapter = get_adapter("mock")
            ctx = AdapterContext(tenant_id=workflow.tenant_id,
                                 supplier_id=service.supplier_id)
            result = adapter.revalidate(ctx, snapshot)
            new_price = (result.get("price") or {}).get("amount")
            if new_price is not None and service.client_total is not None:
                from decimal import Decimal

                if Decimal(str(new_price)) != service.client_total:
                    price_changes.append({
                        "service_id": str(service.id),
                        "old": str(service.client_total),
                        "new": str(new_price), "currency": service.currency,
                    })
        except AdapterError as exc:
            warnings.append({"code": exc.code, "message": "Не удалось перепроверить цену",
                             "service_id": str(service.id)})

    # travel policy / approvals
    for item in items:
        compliance = item.service.policy_compliance or {}
        verdict = compliance.get("verdict")
        if verdict == "forbidden":
            blocking.append({"code": "POLICY_FORBIDDEN",
                             "message": "Вариант запрещён travel policy",
                             "service_id": str(item.service.id)})
        elif verdict == "approval_required":
            approvals.append({"service_id": str(item.service.id),
                              "chain": compliance.get("approver_chain", [])})

    # кредитный лимит / депозит / предоплата (для корпоративных заказов)
    if order.client_company_id:
        settlement = getattr(order.client_company, "settlement", None)
        if settlement is not None and settlement.mode == "deposit":
            from decimal import Decimal

            total = sum((item.service.client_total or Decimal(0) for item in items),
                        Decimal(0))
            available = settlement.deposit_balance - settlement.deposit_reserved
            if total > available:
                blocking.append({
                    "code": "DEPOSIT_INSUFFICIENT",
                    "message": f"Недостаточно депозита: нужно {total}, доступно {available}",
                })

    result = {
        "ok": not blocking,
        "warnings": warnings,
        "blocking_errors": blocking,
        "required_approvals": approvals,
        "price_changes": price_changes,
        "checked_at": timezone.now().isoformat(),
    }
    workflow.preflight_result = result
    workflow.preflight_at = timezone.now()
    workflow.price_confirmation_required = bool(price_changes or warnings)
    if not blocking:
        workflow.status = "preflight_ok"
    workflow.save(update_fields=["preflight_result", "preflight_at",
                                 "price_confirmation_required", "status"])
    return result
