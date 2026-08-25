"""Определение сервисного сбора по финансовым условиям контрагента.

Единый серверный расчёт для внутренней математики бланков: frontend только
передаёт контекст (контрагент, вид услуги, база поставщика) и получает готовую
сумму вместе с источником — договором, дополнительным соглашением и правилом.
Второй системы тарифов не создаём: используются существующие Contract →
Agreement → FeeRule (ТЗ §6.2, §6.3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from common.money import quantize
from crm.models import Agreement, Company, Contract, FeeRule

# Метки видов услуг из интерфейса импорта бланков и из FeeRule.service_kind.
SERVICE_KIND_ALIASES = {
    "авиа": "avia",
    "avia": "avia",
    "flight": "avia",
    "air": "avia",
    "жд": "rail",
    "ж/д": "rail",
    "rail": "rail",
    "train": "rail",
    "гостиница": "hotel",
    "отель": "hotel",
    "hotel": "hotel",
    "трансфер": "transfer",
    "transfer": "transfer",
    "страховка": "insurance",
    "страхование": "insurance",
    "insurance": "insurance",
    "тур": "tour",
    "tour": "tour",
    "автобус": "bus",
    "bus": "bus",
    "виза": "visa",
    "visa": "visa",
    "прочее": "other",
    "other": "other",
}

ACTIVE_STATUSES = {"active", "действующий", "действующая", "signed", ""}


def normalize_service_kind(value) -> str:
    raw = str(value or "").strip().lower()
    return SERVICE_KIND_ALIASES.get(raw, raw)


def to_decimal(value, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _is_active(status: str) -> bool:
    return str(status or "").strip().lower() in ACTIVE_STATUSES


def _contract_covers(contract: Contract, on_date: date) -> bool:
    if not _is_active(contract.status):
        return False
    if contract.starts_at and contract.starts_at > on_date:
        return False
    if contract.ends_at and contract.ends_at < on_date:
        return False
    return True


def _agreement_covers(agreement: Agreement, on_date: date) -> bool:
    if not _is_active(agreement.status):
        return False
    if agreement.effective_from and agreement.effective_from > on_date:
        return False
    if agreement.effective_to and agreement.effective_to < on_date:
        return False
    return True


def active_agreement(contract: Contract, on_date: date) -> Agreement | None:
    """Актуальная версия условий договора на дату: максимальная версия среди действующих."""
    agreements = [
        agreement
        for agreement in contract.agreements.filter(archived_at__isnull=True)
        if _agreement_covers(agreement, on_date)
    ]
    if not agreements:
        return None
    return max(agreements, key=lambda item: (item.agreement_version, item.created_at))


def applicable_fee_rule(
    company: Company,
    *,
    service_kind: str,
    on_date: date | None = None,
    fee_kind: str = FeeRule.FeeKind.SERVICE,
) -> tuple[FeeRule, Agreement, Contract] | None:
    """Первое подходящее правило сбора по действующему договору контрагента."""
    on_date = on_date or date.today()
    contracts = [
        contract
        for contract in company.contracts.filter(archived_at__isnull=True)
        .prefetch_related("agreements__fee_rules")
        .order_by("-signed_at", "-created_at")
        if _contract_covers(contract, on_date)
    ]
    for contract in contracts:
        agreement = active_agreement(contract, on_date)
        if agreement is None:
            continue
        rules = [
            rule
            for rule in agreement.fee_rules.filter(archived_at__isnull=True)
            if rule.service_kind == service_kind and rule.fee_kind == fee_kind
        ]
        if not rules:
            continue
        rule = max(rules, key=lambda item: item.created_at)
        return rule, agreement, contract
    return None


def _manual(reason: str, **context) -> dict:
    return {"fee": None, "source": "manual", "reason": reason, **context}


def resolve_service_fee(
    *,
    company: Company | None,
    service_kind,
    base_amount=Decimal("0"),
    currency: str = "",
    on_date: date | None = None,
    fee_kind: str = FeeRule.FeeKind.SERVICE,
) -> dict:
    """Считает сервисный сбор бланка по финансовым условиям контрагента.

    Возвращает либо договорной сбор с источником, либо признак ручного ввода с
    причиной: нулевой сбор «по умолчанию» здесь не подставляется никогда —
    оператор должен видеть, что автоматического правила нет.
    """
    kind = normalize_service_kind(service_kind)
    document_currency = str(currency or "").upper()[:3]
    if company is None:
        return _manual("no_company", service_kind=kind, currency=document_currency)
    if not kind:
        return _manual("unknown_service_kind", currency=document_currency)

    on_date = on_date or date.today()
    found = applicable_fee_rule(company, service_kind=kind, on_date=on_date, fee_kind=fee_kind)
    if found is None:
        has_contract = any(
            _contract_covers(contract, on_date)
            for contract in company.contracts.filter(archived_at__isnull=True)
        )
        reason = "no_applicable_rule" if has_contract else "no_active_contract"
        return _manual(reason, service_kind=kind, currency=document_currency, company_id=str(company.id))

    rule, agreement, contract = found
    base = to_decimal(base_amount)
    rule_currency = (rule.currency or "").upper()[:3]
    common = {
        "service_kind": kind,
        "calculation": rule.calculation,
        "value": str(rule.value),
        "rule_id": str(rule.id),
        "agreement_id": str(agreement.id),
        "contract_id": str(contract.id),
        "contract_number": contract.number,
        "agreement_number": agreement.number,
        "agreement_version": agreement.agreement_version,
        "company_id": str(company.id),
        "description": rule.description or "",
    }
    if rule.calculation == FeeRule.Calculation.PERCENT:
        target_currency = document_currency or rule_currency or "RUB"
        fee = quantize(base * rule.value / Decimal(100), target_currency)
        return {"fee": str(fee), "currency": target_currency, "source": "contract", **common}

    # Фиксированный сбор указан в валюте договора. Конвертацию не выдумываем:
    # при другой валюте бланка оператор ставит сумму вручную, но видит условие.
    if document_currency and rule_currency and document_currency != rule_currency:
        return _manual(
            "currency_mismatch",
            contract_fee=str(quantize(rule.value, rule_currency)),
            contract_currency=rule_currency,
            currency=document_currency,
            **common,
        )
    target_currency = rule_currency or document_currency or "RUB"
    fee = quantize(rule.value, target_currency)
    return {"fee": str(fee), "currency": target_currency, "source": "contract", **common}
