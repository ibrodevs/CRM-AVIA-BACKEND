from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from common.audit import audit
from common.errors import ApiError, BusinessRejectionError
from common.money import quantize
from common.outbox import emit_event
from finance.models import (
    FinancialAccount,
    FinancialObligation,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    PaymentAllocation,
    Refund,
)


def _system_account(tenant_id, kind: str, currency: str) -> FinancialAccount:
    account, _ = FinancialAccount.objects.get_or_create(
        tenant_id=tenant_id,
        code=f"SYS-{kind.upper()}",
        currency=currency,
        defaults={"name": f"Системный счёт {kind}", "kind": kind},
    )
    return account


def post_ledger(
    tenant_id, *, kind: str, description: str, entries: list[dict], order=None, payment=None, user=None
) -> LedgerTransaction:
    """Создаёт сбалансированную группу проводок. entries: [{account, direction, amount, currency}]."""
    by_currency: dict[str, Decimal] = {}
    for entry in entries:
        sign = Decimal(1) if entry["direction"] == "debit" else Decimal(-1)
        by_currency[entry["currency"]] = (
            by_currency.get(entry["currency"], Decimal(0)) + sign * entry["amount"]
        )
    unbalanced = {c: v for c, v in by_currency.items() if v != 0}
    if unbalanced:
        raise BusinessRejectionError(
            code="LEDGER_UNBALANCED",
            message="Сумма дебета не равна сумме кредита",
            details={"unbalanced": {c: str(v) for c, v in unbalanced.items()}},
        )
    ledger_transaction = LedgerTransaction.objects.create(
        tenant_id=tenant_id,
        occurred_at=timezone.now(),
        description=description,
        kind=kind,
        order=order,
        payment=payment,
        posted_by=user,
    )
    LedgerEntry.objects.bulk_create(
        [
            LedgerEntry(
                transaction=ledger_transaction,
                account=entry["account"],
                direction=entry["direction"],
                amount=entry["amount"],
                currency=entry["currency"],
            )
            for entry in entries
        ]
    )
    return ledger_transaction


@transaction.atomic
def confirm_payment(
    *, payment_id, user, allocations: list[dict] | None = None, expected_version=None, request=None
) -> Payment:
    """Подтверждение платежа: optimistic lock, ledger, allocations, события."""
    from common.locking import check_version

    payment = Payment.objects.select_for_update().get(pk=payment_id)
    check_version(payment, expected_version)
    if payment.status == Payment.Status.CONFIRMED:
        return payment
    if payment.status not in (Payment.Status.DRAFT, Payment.Status.PENDING):
        raise ApiError(
            code="PAYMENT_NOT_CONFIRMABLE", message=f"Платёж в статусе {payment.status}", status_code=409
        )

    payment.status = Payment.Status.CONFIRMED
    payment.confirmed_at = timezone.now()
    payment.confirmed_by = user
    payment.version += 1
    payment.save(update_fields=["status", "confirmed_at", "confirmed_by", "version", "updated_at"])

    cash = _system_account(payment.tenant_id, "bank", payment.currency)
    receivable = _system_account(payment.tenant_id, "client_receivable", payment.currency)
    if payment.direction == Payment.Direction.INCOMING:
        entries = [
            {"account": cash, "direction": "debit", "amount": payment.amount, "currency": payment.currency},
            {
                "account": receivable,
                "direction": "credit",
                "amount": payment.amount,
                "currency": payment.currency,
            },
        ]
    else:
        payable = _system_account(payment.tenant_id, "supplier_payable", payment.currency)
        entries = [
            {
                "account": payable,
                "direction": "debit",
                "amount": payment.amount,
                "currency": payment.currency,
            },
            {"account": cash, "direction": "credit", "amount": payment.amount, "currency": payment.currency},
        ]
    post_ledger(
        payment.tenant_id,
        kind="payment",
        description=f"Платёж {payment.pk}",
        entries=entries,
        order=payment.order,
        payment=payment,
        user=user,
    )

    for allocation in allocations or []:
        allocate_payment(
            payment=payment,
            obligation_id=allocation["obligation"],
            amount=Decimal(str(allocation["amount"])),
            user=user,
        )

    emit_event(
        "order.updated",
        payment.order or payment,
        payload={"action": "payment_confirmed", "payment": str(payment.pk)},
    )
    audit(
        "finance.payment_confirmed",
        actor=user,
        resource=payment,
        request=request,
        after={"amount": str(payment.amount), "currency": payment.currency},
    )
    _maybe_mark_order_paid(payment.order, user)
    return payment


def allocate_payment(*, payment: Payment, obligation_id, amount: Decimal, user) -> PaymentAllocation:
    if payment.status != Payment.Status.CONFIRMED:
        raise ApiError(
            code="PAYMENT_NOT_CONFIRMED",
            message="Распределять можно только подтверждённый платёж",
            status_code=409,
        )
    obligation = FinancialObligation.objects.select_for_update().get(pk=obligation_id)
    if obligation.tenant_id != payment.tenant_id:
        raise ApiError(code="NOT_FOUND", message="Обязательство не найдено", status_code=404)
    if payment.order_id and obligation.order_id and obligation.order_id != payment.order_id:
        raise BusinessRejectionError(
            code="ORDER_MISMATCH",
            message="Платёж нельзя распределить на обязательство другого заказа",
        )
    allowed_directions = {
        Payment.Direction.INCOMING: {
            FinancialObligation.Direction.CLIENT_RECEIVABLE,
            FinancialObligation.Direction.SUPPLIER_REFUND,
        },
        Payment.Direction.OUTGOING: {
            FinancialObligation.Direction.SUPPLIER_PAYABLE,
            FinancialObligation.Direction.CLIENT_REFUND,
        },
    }
    if obligation.direction not in allowed_directions[payment.direction]:
        raise BusinessRejectionError(
            code="OBLIGATION_DIRECTION_MISMATCH",
            message="Направление платежа не соответствует типу обязательства",
            details={"payment_direction": payment.direction, "obligation_direction": obligation.direction},
        )
    if obligation.currency != payment.currency:
        raise BusinessRejectionError(
            code="CURRENCY_MISMATCH", message="Валюта платежа и обязательства различаются"
        )
    allocated = payment.allocations.aggregate(total=models.Sum("amount"))["total"] or Decimal(0)
    if allocated + amount > payment.amount:
        raise BusinessRejectionError(
            code="ALLOCATION_EXCEEDS_PAYMENT",
            message="Сумма распределений превышает платёж",
            details={"payment_amount": str(payment.amount), "allocated": str(allocated)},
        )
    if obligation.paid_amount + amount > obligation.original_amount:
        raise BusinessRejectionError(
            code="ALLOCATION_EXCEEDS_OBLIGATION",
            message="Оплата превышает обязательство",
            details={"outstanding": str(obligation.outstanding_amount)},
        )
    allocation = PaymentAllocation.objects.create(
        tenant_id=payment.tenant_id,
        payment=payment,
        obligation=obligation,
        amount=amount,
        created_by=user,
    )
    obligation.paid_amount += amount
    obligation.status = (
        FinancialObligation.Status.SETTLED
        if obligation.paid_amount >= obligation.original_amount
        else FinancialObligation.Status.PARTIAL
    )
    obligation.save(update_fields=["paid_amount", "status", "updated_at"])
    return allocation


def _maybe_mark_order_paid(order, user) -> None:
    """paid рассчитывается из финансов (ТЗ §7.2)."""
    if order is None:
        return
    from orders.models import Order

    if order.status != Order.Status.AWAITING_PAYMENT:
        return
    outstanding = FinancialObligation.objects.filter(
        order=order,
        direction=FinancialObligation.Direction.CLIENT_RECEIVABLE,
        status__in=["open", "partial"],
    ).exists()
    if not outstanding:
        from orders.services import transition_order

        transition_order(
            order_id=order.pk,
            user=user,
            target_status=Order.Status.PAID,
            reason="Оплата полностью распределена",
            expected_version=Order.objects.get(pk=order.pk).version,
        )


def build_refund(
    *,
    tenant_id,
    currency: str,
    original_paid: Decimal,
    supplier_penalty: Decimal = Decimal(0),
    agency_service_fee: Decimal = Decimal(0),
    other_withholdings: Decimal = Decimal(0),
    payment: Payment | None = None,
    obligation=None,
    aftersale_case=None,
    user=None,
) -> Refund:
    """refund = original_paid - penalty - fee - withholdings; >= 0 и <= остатка (ТЗ §14.4)."""
    refund_amount = quantize(
        original_paid - supplier_penalty - agency_service_fee - other_withholdings,
        currency,
    )
    if refund_amount < 0:
        refund_amount = Decimal(0)
    if payment is not None:
        already = payment.refunds.filter(status__in=["approved", "executed"]).aggregate(
            total=models.Sum("refund_amount")
        )["total"] or Decimal(0)
        available = payment.amount - already
        if refund_amount > available:
            raise BusinessRejectionError(
                code="REFUND_EXCEEDS_PAID",
                message="Возврат превышает фактически оплаченный не возвращённый остаток",
                details={"available": str(available)},
            )
    return Refund.objects.create(
        tenant_id=tenant_id,
        payment=payment,
        obligation=obligation,
        aftersale_case=aftersale_case,
        currency=currency,
        original_paid=original_paid,
        supplier_penalty=supplier_penalty,
        agency_service_fee=agency_service_fee,
        other_withholdings=other_withholdings,
        refund_amount=refund_amount,
        formula_snapshot={
            "formula": "refund = original_paid - supplier_penalty - agency_service_fee "
            "- other_withholdings, min 0, max unrefunded paid",
            "components": {
                "original_paid": str(original_paid),
                "supplier_penalty": str(supplier_penalty),
                "agency_service_fee": str(agency_service_fee),
                "other_withholdings": str(other_withholdings),
            },
            "result": str(refund_amount),
            "rounding": "ROUND_HALF_UP",
        },
        created_by=user,
    )


@transaction.atomic
def execute_refund(*, refund_id, user, request=None) -> Refund:
    refund = Refund.objects.select_for_update().get(pk=refund_id)
    if refund.status == Refund.Status.EXECUTED:
        return refund
    if refund.status not in (Refund.Status.DRAFT, Refund.Status.APPROVED):
        raise ApiError(
            code="REFUND_NOT_EXECUTABLE", message=f"Возврат в статусе {refund.status}", status_code=409
        )
    refund.status = Refund.Status.EXECUTED
    refund.executed_at = timezone.now()
    refund.executed_by = user
    refund.version += 1
    refund.save(update_fields=["status", "executed_at", "executed_by", "version", "updated_at"])
    cash = _system_account(refund.tenant_id, "bank", refund.currency)
    receivable = _system_account(refund.tenant_id, "client_receivable", refund.currency)
    order = None
    if refund.payment_id and refund.payment.order_id:
        order = refund.payment.order
    elif refund.aftersale_case_id:
        order = refund.aftersale_case.order
    if refund.refund_amount > 0:
        post_ledger(
            refund.tenant_id,
            kind="refund",
            description=f"Возврат {refund.pk}",
            entries=[
                {
                    "account": receivable,
                    "direction": "debit",
                    "amount": refund.refund_amount,
                    "currency": refund.currency,
                },
                {
                    "account": cash,
                    "direction": "credit",
                    "amount": refund.refund_amount,
                    "currency": refund.currency,
                },
            ],
            order=order,
            payment=refund.payment,
            user=user,
        )
    if refund.obligation_id:
        obligation = FinancialObligation.objects.select_for_update().get(pk=refund.obligation_id)
        obligation.refunded_amount += refund.refund_amount
        if obligation.refunded_amount >= obligation.original_amount:
            obligation.status = FinancialObligation.Status.SETTLED
        obligation.save(update_fields=["refunded_amount", "status", "updated_at"])
    audit(
        "finance.refund_executed",
        actor=user,
        resource=refund,
        request=request,
        after={"amount": str(refund.refund_amount)},
    )
    emit_event(
        "order.updated",
        order or refund,
        payload={"action": "refund_executed", "refund": str(refund.pk)},
    )
    return refund


@transaction.atomic
def reserve_deposit(
    *, company, amount: Decimal, user, allow_override: bool = False, reason: str = ""
) -> None:
    """Атомарный резерв депозита; отрицательный остаток — только override (ТЗ §14.3)."""
    from crm.models import SettlementProfile

    settlement = SettlementProfile.objects.select_for_update().get(company=company)
    available = settlement.deposit_balance - settlement.deposit_reserved
    if amount > available and not allow_override:
        raise BusinessRejectionError(
            code="DEPOSIT_INSUFFICIENT",
            message="Недостаточно депозита",
            details={"available": str(available), "requested": str(amount)},
        )
    if amount > available and allow_override:
        if not reason:
            raise ApiError(code="REASON_REQUIRED", message="Override требует причины", status_code=400)
        audit(
            "finance.deposit_override",
            actor=user,
            resource=company,
            reason=reason,
            after={"requested": str(amount), "available": str(available)},
        )

        settlement.deposit_reserved = settlement.deposit_balance
        settlement.save(update_fields=["deposit_reserved"])
        return
    settlement.deposit_reserved += amount
    settlement.save(update_fields=["deposit_reserved"])


@transaction.atomic
def release_deposit(*, company, amount: Decimal) -> None:
    from crm.models import SettlementProfile

    settlement = SettlementProfile.objects.select_for_update().get(company=company)
    settlement.deposit_reserved = max(Decimal(0), settlement.deposit_reserved - amount)
    settlement.save(update_fields=["deposit_reserved"])
