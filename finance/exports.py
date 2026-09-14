"""Выгрузка в бухгалтерию и отправка акта сверки контрагенту.

Обе операции раньше возвращали `{"status": "queued"}`, не создавая ни файла, ни
очереди. Здесь выгрузка формирует настоящий XLSX, а отправка акта ставится в
очередь `common.OutboundDelivery` с текстом акта во вложении.
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

_EXPORT_COLUMNS = (
    ("date", "Дата"),
    ("basis", "Основание"),
    ("order", "Заказ"),
    ("kind", "Операция"),
    ("debit", "Дебет"),
    ("credit", "Кредит"),
)


def build_accounting_workbook(counterpart: str, payload: dict) -> bytes:
    """XLSX с операциями по контрагенту — файл, который принимает бухгалтерия."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Операции"

    sheet["A1"] = "Выгрузка в бухгалтерию"
    sheet["A1"].font = Font(bold=True, size=13)
    sheet["A2"] = f"Контрагент: {counterpart}"
    sheet["A3"] = f"Период: {payload.get('period', 'весь период')}"

    header_row = 5
    for index, (_, title) in enumerate(_EXPORT_COLUMNS, start=1):
        cell = sheet.cell(row=header_row, column=index, value=title)
        cell.font = Font(bold=True)

    row_index = header_row + 1
    for row in payload.get("rows", []):
        for column_index, (key, _) in enumerate(_EXPORT_COLUMNS, start=1):
            sheet.cell(row=row_index, column=column_index, value=row.get(key, ""))
        row_index += 1

    totals_row = row_index + 1
    sheet.cell(row=totals_row, column=4, value="Итого").font = Font(bold=True)
    sheet.cell(row=totals_row, column=5, value=payload.get("debit", 0)).font = Font(bold=True)
    sheet.cell(row=totals_row, column=6, value=payload.get("credit", 0)).font = Font(bold=True)
    sheet.cell(row=totals_row + 1, column=4, value="Сальдо").font = Font(bold=True)
    sheet.cell(row=totals_row + 1, column=5, value=payload.get("balance", 0)).font = Font(bold=True)

    for index in range(1, len(_EXPORT_COLUMNS) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 22

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def reconciliation_text(counterpart: str, payload: dict) -> str:
    lines = [
        "Акт сверки взаимных расчётов",
        f"Контрагент: {counterpart}",
        f"Период: {payload.get('period', 'весь период')}",
        f"Дебет: {payload.get('debit', 0)}",
        f"Кредит: {payload.get('credit', 0)}",
        f"Сальдо: {payload.get('balance', 0)}",
        "",
        " | ".join(title for _, title in _EXPORT_COLUMNS),
    ]
    for row in payload.get("rows", []):
        lines.append(" | ".join(str(row.get(key, "")) for key, _ in _EXPORT_COLUMNS))
    return "\n".join(lines)


def resolve_counterpart_email(tenant_id, payload: dict) -> str:
    """E-mail контрагента: из запроса, карточки компании или поставщика."""
    explicit = str(payload.get("email", "")).strip()
    if explicit:
        return explicit
    if company_id := payload.get("company"):
        from crm.models import Company

        company = Company.objects.filter(pk=company_id, tenant_id=tenant_id).first()
        if company and company.email:
            return company.email
    if supplier_id := payload.get("supplier"):
        from suppliers.models import Supplier

        supplier = Supplier.objects.filter(pk=supplier_id, tenant_id=tenant_id).first()
        contact_email = getattr(supplier, "email", "") if supplier else ""
        if contact_email:
            return contact_email
    return ""
