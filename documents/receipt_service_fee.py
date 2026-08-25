"""Источник сервисного сбора подтверждённой квитанции.

Сумму сбора считает crm.fee_resolution; здесь она только перепроверяется на
сервере при подтверждении импорта и сохраняется рядом с документом, чтобы после
перезагрузки страницы было видно, откуда взялся сбор: из договора или от
оператора вручную.
"""

from __future__ import annotations

from crm.fee_resolution import applicable_fee_rule, normalize_service_kind, to_decimal


def _company_for(user, company, order):
    if company is not None:
        return company
    if order is not None and order.client_company_id:
        from crm.models import Company

        return Company.objects.filter(
            tenant_id=user.tenant_id, pk=order.client_company_id, archived_at__isnull=True
        ).first()
    return None


def confirm_service_fee_metadata(
    *,
    user,
    company,
    order,
    submitted,
    service_kind,
    on_date=None,
) -> dict:
    """Возвращает блок service_fee для metadata.receipt_import."""
    payload = submitted if isinstance(submitted, dict) else {}
    amount = to_decimal(payload.get("amount"), None)
    kind = normalize_service_kind(payload.get("service_kind") or service_kind)
    currency = str(payload.get("currency") or "").upper()[:3]
    claimed_source = str(payload.get("source") or "").strip().lower()
    # blanks объясняет, почему сумма документа отличается от значения правила:
    # у группового PDF сбор начисляется на каждый бланк отдельно.
    try:
        blanks = max(1, int(payload.get("blanks") or 1))
    except (TypeError, ValueError):
        blanks = 1
    block = {
        "amount": str(amount) if amount is not None else "",
        "currency": currency,
        "service_kind": kind,
        "blanks": blanks,
        "source": "manual",
    }

    if claimed_source != "contract":
        block["reason"] = str(payload.get("reason") or "manual")
        return block

    target = _company_for(user, company, order)
    found = applicable_fee_rule(target, service_kind=kind, on_date=on_date) if target else None
    if found is None:
        # Условия могли измениться между расчётом и подтверждением: не выдаём
        # сумму за договорную, если правило больше не действует.
        block["reason"] = "rule_not_found"
        return block

    rule, agreement, contract = found
    block.update(
        {
            "source": "contract",
            "calculation": rule.calculation,
            "value": str(rule.value),
            "rule_id": str(rule.id),
            "agreement_id": str(agreement.id),
            "agreement_number": agreement.number,
            "contract_id": str(contract.id),
            "contract_number": contract.number,
            "company_id": str(target.id),
            "verified": True,
        }
    )
    return block
