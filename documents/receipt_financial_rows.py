"""Print newly introduced breakdown rows in the derived supplier PDF."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape


def additional_rows(before, after):
    from documents import receipt_supplier_pdf_patch as supplier

    old_group, new_group = supplier._first_group(before), supplier._first_group(after)
    if old_group or new_group:
        rows = []
        for old, new in zip(old_group, new_group, strict=False):
            for item in additional_rows(old, new):
                source_page = new.get('sourcePage') or new.get('source_page') or new.get('receiptPage')
                page_index = int(source_page) - 1 if str(source_page or '').isdigit() else -1
                item.update(page=page_index, ticket=str(new.get('ticketNo') or new.get('ticket_number') or ''))
                rows.append(item)
        return rows
    rows = []
    for key, title in (("taxBreakdown", "Такса"), ("feeBreakdown", "Сбор"), ("fareBreakdown", "Тариф")):
        if key == "fareBreakdown" and supplier._uses_it_fare(after):
            continue
        old = supplier._value(before, key) or []
        new = supplier._value(after, key) or []
        pairs = {index: row for index, row, _new in supplier._paired_breakdown_rows(old, new)}
        for index, row in enumerate(new):
            if not isinstance(row, dict):
                continue
            amount = supplier._decimal(row.get("amount"))
            # Existing zero rows have no printed box to replace either.
            if amount is None or amount == 0 or (index in pairs and supplier._decimal(pairs[index].get("amount")) not in (None, 0)):
                continue
            code = str(row.get("code") or "").strip()
            label = str(row.get("label") or row.get("name") or "").strip()
            name = " · ".join(dict.fromkeys(value for value in (code, label) if value)) or title
            rows.append({"label": name, "amount": format(amount, '.2f'),
                         "currency": str(row.get("currency") or after.get("currency") or "RUB"), "page": None})
    return rows


def insert_financial_rows(content, rows):
    """Use proven empty space near totals, otherwise add an explicit breakdown page.

    The source pages are never erased, shifted or cropped to make room. Checking
    actual glyph boxes also protects supplier notes and payment terms.
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph

    from documents.receipt_supplier_pdf_writer_fix import _layout_lines

    font = "ReceiptRowsSans"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(Path(__file__).with_name('fonts') / 'NotoSans-Regular.ttf')))
    reader = PdfReader(BytesIO(content), strict=False)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    try:
        layouts = _layout_lines(content)
    except Exception:
        layouts = []
    groups = {}
    for row in rows:
        groups.setdefault((row.get('page'), row.get('ticket', '')), []).append(row)
    inserted, appended = 0, 0
    style = ParagraphStyle('AddedFinancialRow', fontName=font, fontSize=9, leading=13)
    occupied_panels = {}
    for (preferred, _ticket), group in groups.items():
        # Never put one passenger's correction on another passenger's page.
        page_indexes = [preferred] if preferred is not None else range(len(reader.pages))
        placement = None
        for page_index in page_indexes:
            if page_index < 0 or page_index >= len(layouts):
                continue
            page = reader.pages[page_index]
            if page.rotation % 360:
                continue  # Put additions on a separate page rather than misplacing rotated coordinates.
            width, height = float(page.mediabox.width), float(page.mediabox.height)
            lines = layouts[page_index]
            totals = [line for line in lines if any(alias in line['text'].casefold()
                      for alias in ('итого по тарифу', 'всего к оплате', 'total', 'итого'))]
            for anchor in totals:
                panel_width = min(280, width - 40)
                paragraphs = [Paragraph(escape(row['label']), style) for row in group]
                heights = [max(16, paragraph.wrap(panel_width - 110, height)[1] + 5) for paragraph in paragraphs]
                panel_height = sum(heights) + 35 + (18 if group[0].get('ticket') else 0)
                # Try aligned with the total, then progressively below it.
                top = min(anchor['bbox'][3] + 4, height - 24)
                for y in range(int(top - panel_height), max(24, int(top - 150)), -8):
                    for x in (20, width - panel_width - 20):
                        box = (x - 4, y - 4, x + panel_width + 4, y + panel_height + 4)
                        if box[1] < 20 or box[3] > height - 20:
                            continue
                        occupied = any(box[0] < line['bbox'][2] and box[2] > line['bbox'][0]
                                       and box[1] < line['bbox'][3] and box[3] > line['bbox'][1] for line in lines)
                        occupied = occupied or any(box[0] < prior[2] and box[2] > prior[0]
                                                    and box[1] < prior[3] and box[3] > prior[1]
                                                    for prior in occupied_panels.get(page_index, []))
                        if not occupied:
                            placement = (page_index, x, y, panel_width, panel_height, heights, paragraphs)
                            break
                    if placement:
                        break
                if placement:
                    break
            if placement:
                break
        if placement is None:
            # Large additions get their own readable page rather than hiding source content.
            page = writer.add_blank_page(width=595.28, height=841.89)
            width, height = 595.28, 841.89
            x, y, panel_width = 30, height - 70, width - 60
            paragraphs = [Paragraph(escape(row['label']), style) for row in group]
            heights = [max(16, p.wrap(panel_width - 110, height)[1] + 5) for p in paragraphs]
            panel_height = sum(heights) + 35 + (18 if group[0].get('ticket') else 0)
            # Bound the page capacity. The caller must never publish truncated rows.
            if panel_height > height - 100:
                raise ValueError('Слишком много дополнительных строк для одной страницы')
            y -= panel_height
            appended += 1
        else:
            page_index, x, y, panel_width, panel_height, heights, paragraphs = placement
            occupied_panels.setdefault(page_index, []).append((x - 4, y - 4, x + panel_width + 4, y + panel_height + 4))
            page = writer.pages[page_index]
            width, height = float(page.mediabox.width), float(page.mediabox.height)
        overlay = BytesIO()
        pdf = canvas.Canvas(overlay, pagesize=(width, height))
        pdf.setStrokeColorRGB(.84, .87, .91)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.roundRect(x, y, panel_width, panel_height, 6, fill=1, stroke=1)
        pdf.setFillColorRGB(.13, .17, .23)
        pdf.setFont(font, 10)
        pdf.drawString(x + 10, y + panel_height - 18, 'Дополнительные таксы и сборы')
        cursor = y + panel_height - 29
        for row, paragraph, row_height in zip(group, paragraphs, heights, strict=True):
            paragraph.drawOn(pdf, x + 10, cursor - row_height + 5)
            pdf.setFont(font, 9)
            pdf.drawRightString(x + panel_width - 10, cursor - 10, f"{row['amount']} {row['currency']}")
            cursor -= row_height
        if group[0].get('ticket'):
            pdf.setFont(font, 8)
            pdf.drawString(x + 10, y + 9, 'Билет: ' + group[0]['ticket'])
        pdf.save()
        page.merge_page(PdfReader(overlay).pages[0])
        inserted += len(group)
    output = BytesIO()
    writer.write(output)
    return output.getvalue(), {"added_rows": inserted, "breakdown_appended_pages": appended}
