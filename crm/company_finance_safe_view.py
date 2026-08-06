from rest_framework.response import Response

from crm.company_finance_views import (
    CompanyFinancialConditionsView as BaseCompanyFinancialConditionsView,
)
from crm.company_finance_views import _company, serialize_financial_conditions


def _has_complete_conditions(value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    contracts = value.get("contracts")
    if not isinstance(contracts, list):
        return False
    return any(
        isinstance(contract, dict)
        and str(contract.get("no") or "").strip()
        and isinstance(contract.get("agreements"), list)
        and len(contract["agreements"]) > 0
        for contract in contracts
    )


class CompanyFinancialConditionsView(BaseCompanyFinancialConditionsView):
    """Не считает служебный пустой SettlementProfile настроенными условиями."""

    def get(self, request, company_id):
        company = _company(request, company_id)
        value = serialize_financial_conditions(company)
        configured = _has_complete_conditions(value)
        return Response({"configured": configured, "value": value if configured else None})
