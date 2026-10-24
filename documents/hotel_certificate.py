"""A CRM certificate of the last known HBPRO booking, never a supplier original."""

import hashlib
import json
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from common.errors import ApiError
from documents.models import Document
from documents.services import add_document_version
from integrations.hotelbook_observability import normalized_status


def certificate_status(snapshot):
    if snapshot.get("provider_adapter") != "hotelbook":
        return "unsupported"
    booking = snapshot.get("hotelbook_booking") or {}
    items = booking.get("items") or []
    if not booking.get("order_id") or not items or not all(i.get("itemId") for i in items):
        return "pending"
    status = normalized_status(items)
    if booking.get("state") == "cancelling" or booking.get("cancel_requested_ids") and status != "cancelled":
        return "pending"
    return status if status in ("confirmed", "cancelled") else "pending"


def hotel_capabilities(snapshot):
    if snapshot.get("provider_adapter") != "hotelbook":
        return None
    return {"content_available": True, "certificate_status": certificate_status(snapshot)}


def certificate_data(service):
    snapshot = service.provider_snapshot or {}
    status = certificate_status(snapshot)
    if status not in ("confirmed", "cancelled"):
        raise ApiError(
            code="BOOKING_NOT_CONFIRMED",
            message="Сертификат доступен после подтверждения брони. Обновите статус бронирования.",
            status_code=409,
        )
    hb = snapshot["hotelbook"]
    offer, booking = hb["offer"], snapshot["hotelbook_booking"]
    details = (hb.get("hotel") or {}).get("details") or {}
    guests = []
    for row in service.passengers.select_related("participant__person").filter(status="active"):
        participant = row.participant
        if participant.person:
            person = participant.person
            name = " ".join(filter(None, [person.latin_surname, person.latin_given_name])) or person.full_name
        else:
            guest = participant.guest_snapshot or {}
            name = (
                " ".join(
                    filter(
                        None,
                        [
                            guest.get("latin_surname") or guest.get("surname"),
                            guest.get("latin_given_name") or guest.get("given_name"),
                        ],
                    )
                )
                or guest.get("full_name")
                or guest.get("name")
            )
        if name:
            guests.append(name)
    return {
        "demo": bool(snapshot.get("local_demo")),
        "order_number": service.order.number,
        "status": status,
        "hotel": (hb.get("hotel") or {}).get("name")
        or snapshot.get("itinerary", {}).get("property_name")
        or service.title,
        "address": details.get("address", ""),
        "phone": details.get("phone", ""),
        "check_in": offer.get("checkIn", ""),
        "check_out": offer.get("checkOut", ""),
        "check_in_time": details.get("earliestCheckInTime"),
        "check_out_time": details.get("latestCheckOutTime"),
        "rooms": [
            {
                "name": r.get("roomName") or offer.get("roomGroupName") or "Категория поставщика",
                "quantity": r.get("quantity", 1),
            }
            for r in offer.get("rooms", [])
        ],
        "meal": (offer.get("meal") or {}).get("description", ""),
        "guests": guests,
        "supplier_order": str(booking.get("order_id") or ""),
        "references": [
            str((item.get("providerData") or {}).get("providerReferenceId") or item.get("itemId") or "")
            for item in booking.get("items", [])
        ],
        "synced_at": booking.get("updated_at", ""),
    }


def render_certificate(data):
    font_dir = Path(__file__).with_name("fonts")
    for name, filename in (("HotelSans", "NotoSans-Regular.ttf"), ("HotelSansBold", "NotoSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="HotelBody",
            fontName="HotelSans",
            fontSize=10,
            leading=15,
            spaceAfter=8,
            textColor=colors.HexColor("#243247"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="HotelTitle",
            fontName="HotelSansBold",
            fontSize=21,
            leading=28,
            spaceAfter=14,
            textColor=colors.HexColor("#16324f"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="HotelStatus",
            parent=styles["HotelBody"],
            fontName="HotelSansBold",
            textColor=colors.HexColor("#a32929" if data["status"] == "cancelled" else "#146b4b"),
        )
    )
    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=44,
        leftMargin=44,
        topMargin=42,
        bottomMargin=55,
        title="Сертификат на проживание",
        author="CRM",
    )

    def p(text, style="HotelBody"):
        return Paragraph(escape(str(text or "Не указано")).replace("\n", "<br/>"), styles[style])

    content = [
        p("СЕРТИФИКАТ НА ПРОЖИВАНИЕ", "HotelTitle"),
        p(
            "ОТМЕНЕНО - НЕ ДЕЙСТВУЕТ ДЛЯ ЗАСЕЛЕНИЯ"
            if data["status"] == "cancelled"
            else "Бронирование подтверждено",
            "HotelStatus",
        ),
        p(data["hotel"], "HotelTitle"),
    ]
    if data.get("demo"):
        content.insert(0, p("ДЕМОНСТРАЦИЯ - РЕАЛЬНОГО БРОНИРОВАНИЯ НЕТ", "HotelStatus"))
    rows = [
        ("Заказ CRM", data["order_number"]),
        ("Заказ HBPRO", data["supplier_order"]),
        ("Номер подтверждения", ", ".join(data["references"])),
        ("Адрес гостиницы", data["address"]),
        ("Телефон гостиницы", data["phone"]),
        ("Заезд", " ".join(filter(None, [data["check_in"], data["check_in_time"]]))),
        ("Выезд", " ".join(filter(None, [data["check_out"], data["check_out_time"]]))),
        ("Номера", "\n".join(f"{r['name']} x {r['quantity']}" for r in data["rooms"])),
        ("Питание", data["meal"]),
        ("Гости", "\n".join(data["guests"])),
        ("Статус по данным на", data["synced_at"]),
    ]
    table = Table(
        [[p(label), p(value)] for label, value in rows], colWidths=[150, 357], hAlign="LEFT", splitInRow=1
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef3f8")),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce3ed")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    content += [
        table,
        Spacer(1, 20),
        p(
            "Сформировано CRM по последним сохранённым данным HBPRO. Это документ CRM, а не оригинальный ваучер поставщика. Изменения брони после указанного времени в документе не отражены."
        ),
    ]

    def footer(canvas, document):
        canvas.setFont("HotelSans", 8)
        canvas.setFillColor(colors.HexColor("#68778b"))
        canvas.drawString(44, 28, "CRM / HBPRO - Сертификат на проживание")
        canvas.drawRightString(A4[0] - 44, 28, str(document.page))

    doc.build(content, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def ensure_certificate(service, user):
    from services.models import OrderService

    with transaction.atomic():
        service = (
            OrderService.objects.select_for_update()
            .select_related("order")
            .get(pk=service.pk, tenant_id=service.tenant_id)
        )
        data = certificate_data(service)
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        document = Document.objects.filter(
            service=service,
            tenant_id=service.tenant_id,
            kind="voucher",
            source="generated",
            metadata__generator="hbpro_certificate",
            archived_at__isnull=True,
        ).first()
        if document and document.metadata.get("fingerprint") == fingerprint and document.current_version:
            return document
        if document is None:
            document = Document.objects.create(
                tenant_id=service.tenant_id,
                order_id=service.order_id,
                service=service,
                kind="voucher",
                source="generated",
                title="Сертификат на проживание",
                document_date=timezone.localdate(),
            )
        document.title = "Сертификат на проживание" + (" - ОТМЕНЕНО" if data["status"] == "cancelled" else "")
        document.metadata = {
            "generator": "hbpro_certificate",
            "fingerprint": fingerprint,
            "booking_status": data["status"],
            "supplier_original": False,
        }
        document.save(update_fields=["title", "metadata"])
        add_document_version(
            document,
            content=render_certificate(data),
            mime="application/pdf",
            name=f"hotel-certificate-{service.id}.pdf",
            user=user,
            origin="generated",
        )
        return document
