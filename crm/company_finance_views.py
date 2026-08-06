from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from crm.models import Agreement, Company, Contract, FeeRule, FeeTemplate, SettlementProfile


MODE_IN = {
    "предоплата": SettlementProfile.Mode.PREPAYMENT,
    "prepayment": SettlementProfile.Mode.PREPAYMENT,
    "депозит": SettlementProfile.Mode.DEPOSIT,
    "deposit": SettlementProfile.Mode.DEPOSIT,
    "отсрочка": SettlementProfile.Mode.CREDIT,
    "credit": SettlementProfile.Mode.CREDIT,
}
MODE_OUT = {
    SettlementProfile.Mode.PREPAYMENT: "предоплата",
    SettlementProfile.Mode.DEPOSIT: "депозит",
    SettlementProfile.Mode.CREDIT: "отсрочка",
}

STATUS_IN = {
    "Действующий": "active",
    "active": "active",
    "Архив": "archived",
    "Архивный": "archived",
    "archived": "archived",
    "Черновик": "draft",
    "draft": "draft",
}
STATUS_OUT = {"active": "Действующий", "archived": "Архив", "draft": "Черновик"}

SERVICE_IN = {
    "Авиа": "avia",
    "avia": "avia",
    "ЖД": "rail",
    "rail": "rail",
    "Гостиница": "hotel",
    "Отель": "hotel",
    "hotel": "hotel",
    "Трансфер": "transfer",
    "transfer": "transfer",
    "Страховка": "insurance",
    "insurance": "insurance",
    "Тур": "tour",
    "tour": "tour",
}
SERVICE_OUT = {
    "avia": "Авиа",
    "rail": "ЖД",
    "hotel": "Гостиница",
    "transfer": "Трансфер",
    "insurance": "Страховка",
    "tour": "Тур",
}

TEMPLATE_NAMES = {
    "standard": "Стандартный",
    "deposit": "Депозитный",
    "credit": "Отсрочка",
    "zero": "Без сборов",
}
TEMPLATE_IDS = {value: key for key, value in TEMPLATE_NAMES.items()}


def _company(request, company_id) -> Company:
    company = Company.objects.filter(
        tenant_id=request.user.tenant_id,
        pk=company_id,
        archived_at__isnull=True,
    ).first()
    if company is None:
        raise ApiError(code="NOT_FOUND", message="Компания не найдена", status_code=404)
    return company


def _require_change(request) -> None:
    if not has_permission(request.user, "crm.change") or not has_permission(request.user, "finance.view"):
        raise ApiError(
            code="PERMISSION_DENIED",
            message="Нужны права crm.change и finance.view",
            status_code=403,
        )


def _decimal(value, field: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError):
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректные финансовые данные",
            fields={field: ["Введите корректное число"]},
            status_code=400,
        )
    if result < minimum:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректные финансовые данные",
            fields={field: [f"Значение не может быть меньше {minimum}"]},
            status_code=400,
        )
    return result


def _parse_date(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ApiError(
        code="VALIDATION_ERROR",
        message="Некорректная дата",
        fields={field: ["Используйте дату в формате ДД.ММ.ГГГГ"]},
        status_code=400,
    )


def _is_uuid(value) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _number(value) -> float:
    return float(value or 0)


def _date_text(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _datetime_text(value) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _template_for(tenant_id, template_id: str, user) -> FeeTemplate:
    name = TEMPLATE_NAMES.get(template_id, template_id or "Индивидуальный")
    template = FeeTemplate.objects.filter(
        tenant_id=tenant_id,
        archived_at__isnull=True,
        name=name,
    ).first()
    if template is None:
        template = FeeTemplate.objects.create(
            tenant_id=tenant_id,
            name=name,
            description=f"Шаблон финансовых условий: {name}",
            created_by=user,
        )
    return template


def _serialize_agreement(agreement: Agreement, versions: list[Agreement]) -> dict:
    fees: dict[str, dict] = {}
    fee_descs: dict[str, dict] = {}
    for rule in agreement.fee_rules.all():
        service = SERVICE_OUT.get(rule.service_kind, rule.service_kind)
        fees.setdefault(service, {})[rule.fee_kind] = {
            "type": rule.calculation,
            "value": _number(rule.value),
        }
        fee_descs.setdefault(service, {})[rule.fee_kind] = rule.description or ""

    history = []
    for version in sorted(versions, key=lambda item: item.agreement_version):
        if version.agreement_version > agreement.agreement_version:
            continue
        history.append(
            {
                "date": _datetime_text(version.created_at),
                "user": str(version.created_by) if version.created_by else "Оператор",
                "title": f"{version.number} · {'создано' if version.agreement_version == 1 else 'изменение условий'}",
                "fields": ["Финансовые условия и правила сборов"],
            }
        )

    template_name = agreement.fee_template.name if agreement.fee_template else "Стандартный"
    return {
        "id": str(agreement.id),
        "no": agreement.number,
        "date": _date_text(agreement.effective_from),
        "version": agreement.agreement_version,
        "status": STATUS_OUT.get(agreement.status, agreement.status),
        "template": TEMPLATE_IDS.get(template_name, template_name),
        "templateName": template_name,
        "fees": fees,
        "descs": agreement.service_descriptions if isinstance(agreement.service_descriptions, dict) else {},
        "feeDescs": agreement.fee_descriptions if isinstance(agreement.fee_descriptions, dict) else fee_descs,
        "history": history,
    }


def serialize_financial_conditions(company: Company) -> dict | None:
    settlement = SettlementProfile.objects.filter(
        tenant_id=company.tenant_id,
        company=company,
        archived_at__isnull=True,
    ).first()
    contracts = list(
        company.contracts.filter(archived_at__isnull=True)
        .prefetch_related("agreements__fee_rules", "agreements__fee_template", "agreements__created_by")
        .order_by("-created_at")
    )
    if settlement is None and not contracts:
        return None

    mode = settlement.mode if settlement else SettlementProfile.Mode.PREPAYMENT
    data = {
        "settlement": MODE_OUT.get(mode, mode),
        "currency": settlement.currency if settlement else "USD",
        "deposit": None,
        "credit": None,
        "contracts": [],
    }
    if mode == SettlementProfile.Mode.DEPOSIT and settlement:
        data["deposit"] = {
            "balance": _number(settlement.deposit_balance),
            "reserved": _number(settlement.deposit_reserved),
            "history": [],
        }
    if mode == SettlementProfile.Mode.CREDIT and settlement:
        data["credit"] = {
            "limit": _number(settlement.credit_limit),
            "termDays": settlement.credit_days,
            "debt": 0,
            "overdue": 0,
        }

    for contract in contracts:
        versions = list(contract.agreements.filter(archived_at__isnull=True).order_by("agreement_version"))
        data["contracts"].append(
            {
                "id": str(contract.id),
                "no": contract.number,
                "date": _date_text(contract.signed_at or contract.starts_at),
                "status": STATUS_OUT.get(contract.status, contract.status),
                "agreements": [_serialize_agreement(item, versions) for item in versions],
            }
        )
    return data


def _sync_agreement(request, contract: Contract, payload: dict, currency: str) -> Agreement:
    agreement = None
    raw_id = payload.get("id")
    if _is_uuid(raw_id):
        agreement = contract.agreements.filter(pk=raw_id, archived_at__isnull=True).first()
    version = int(payload.get("version") or payload.get("agreement_version") or 1)
    if agreement is None:
        agreement = contract.agreements.filter(
            agreement_version=version,
            archived_at__isnull=True,
        ).first()
    if agreement is None:
        agreement = Agreement(
            tenant_id=request.user.tenant_id,
            contract=contract,
            agreement_version=version,
            created_by=request.user,
        )

    template_id = str(payload.get("template") or "standard")
    agreement.number = str(payload.get("no") or payload.get("number") or f"ДС № {version}").strip()
    agreement.status = STATUS_IN.get(str(payload.get("status") or "Действующий"), "active")
    agreement.effective_from = _parse_date(payload.get("date") or payload.get("effective_from"), "agreement.date")
    agreement.effective_to = _parse_date(payload.get("effective_to"), "agreement.effective_to")
    agreement.fee_template = _template_for(request.user.tenant_id, template_id, request.user)
    agreement.service_descriptions = payload.get("descs") or payload.get("service_descriptions") or {}
    agreement.fee_descriptions = payload.get("feeDescs") or payload.get("fee_descriptions") or {}
    agreement.updated_by = request.user
    agreement.save()

    agreement.fee_rules.all().delete()
    fees = payload.get("fees") or {}
    fee_descs = agreement.fee_descriptions if isinstance(agreement.fee_descriptions, dict) else {}
    for service_label, rules in fees.items():
        service_kind = SERVICE_IN.get(service_label, str(service_label).lower())
        for fee_kind, rule in (rules or {}).items():
            calculation = str((rule or {}).get("type") or (rule or {}).get("calculation") or "fixed")
            if calculation not in (FeeRule.Calculation.FIXED, FeeRule.Calculation.PERCENT):
                calculation = FeeRule.Calculation.FIXED
            description = ((fee_descs.get(service_label) or {}).get(fee_kind) or "") if isinstance(fee_descs, dict) else ""
            FeeRule.objects.create(
                tenant_id=request.user.tenant_id,
                agreement=agreement,
                service_kind=service_kind,
                fee_kind=fee_kind,
                calculation=calculation,
                value=_decimal((rule or {}).get("value", 0), f"fees.{service_label}.{fee_kind}"),
                currency=currency if calculation == FeeRule.Calculation.FIXED else "",
                description=description,
                created_by=request.user,
            )
    return agreement


def _sync_contract(request, company: Company, payload: dict, currency: str) -> Contract:
    number = str(payload.get("no") or payload.get("number") or "").strip()
    if not number:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Укажите номер договора",
            fields={"contract.number": ["Обязательное поле"]},
            status_code=400,
        )
    contract = None
    raw_id = payload.get("id")
    if _is_uuid(raw_id):
        contract = company.contracts.filter(pk=raw_id, archived_at__isnull=True).first()
    if contract is None:
        contract = company.contracts.filter(number=number, archived_at__isnull=True).first()
    if contract is None:
        contract = Contract(
            tenant_id=request.user.tenant_id,
            company=company,
            created_by=request.user,
        )

    signed_at = _parse_date(payload.get("date") or payload.get("signed_at"), "contract.date")
    contract.number = number
    contract.signed_at = signed_at
    contract.starts_at = _parse_date(payload.get("starts_at"), "contract.starts_at") or signed_at
    contract.ends_at = _parse_date(payload.get("ends_at"), "contract.ends_at")
    contract.status = STATUS_IN.get(str(payload.get("status") or "Действующий"), "active")
    contract.updated_by = request.user
    contract.save()

    for agreement_payload in payload.get("agreements") or []:
        _sync_agreement(request, contract, agreement_payload, currency)
    return contract


class CompanyFinancialConditionsView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        company = _company(request, company_id)
        value = serialize_financial_conditions(company)
        return Response({"configured": value is not None, "value": value})

    @transaction.atomic
    def put(self, request, company_id):
        _require_change(request)
        company = _company(request, company_id)
        payload = request.data.get("value") if isinstance(request.data.get("value"), dict) else request.data

        raw_mode = str(payload.get("settlement") or payload.get("mode") or "предоплата").lower()
        mode = MODE_IN.get(raw_mode)
        if mode is None:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Неизвестный тип взаиморасчётов",
                fields={"settlement": ["Выберите предоплату, депозит или отсрочку"]},
                status_code=400,
            )
        currency = str(payload.get("currency") or "USD").upper()[:3]
        deposit = payload.get("deposit") or {}
        credit = payload.get("credit") or {}
        deposit_balance = _decimal(deposit.get("balance", 0), "deposit.balance")
        deposit_reserved = _decimal(deposit.get("reserved", 0), "deposit.reserved")
        if mode == SettlementProfile.Mode.DEPOSIT and deposit_reserved > deposit_balance:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Резерв не может превышать депозит",
                fields={"deposit.reserved": ["Уменьшите резерв или увеличьте баланс"]},
                status_code=400,
            )
        credit_limit = _decimal(credit.get("limit", 0), "credit.limit")
        credit_days = int(credit.get("termDays") or credit.get("credit_days") or 0)
        if credit_days < 0:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Некорректный срок отсрочки",
                fields={"credit.termDays": ["Срок не может быть отрицательным"]},
                status_code=400,
            )

        settlement, _ = SettlementProfile.objects.get_or_create(
            tenant_id=request.user.tenant_id,
            company=company,
            defaults={"created_by": request.user},
        )
        settlement.mode = mode
        settlement.currency = currency
        settlement.deposit_balance = deposit_balance if mode == SettlementProfile.Mode.DEPOSIT else Decimal("0")
        settlement.deposit_reserved = deposit_reserved if mode == SettlementProfile.Mode.DEPOSIT else Decimal("0")
        settlement.credit_limit = credit_limit if mode == SettlementProfile.Mode.CREDIT else Decimal("0")
        settlement.credit_days = credit_days if mode == SettlementProfile.Mode.CREDIT else 0
        settlement.updated_by = request.user
        settlement.save()

        contracts = payload.get("contracts") or []
        if not contracts:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Добавьте договор",
                fields={"contracts": ["Нужен хотя бы один договор"]},
                status_code=400,
            )
        for contract_payload in contracts:
            _sync_contract(request, company, contract_payload, currency)

        audit("crm.company_financial_conditions_saved", actor=request.user, resource=company, request=request)
        value = serialize_financial_conditions(company)
        return Response({"configured": True, "value": value})

    post = put
