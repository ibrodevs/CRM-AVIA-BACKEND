"""Read-only supplier content and local accommodation certificate actions."""

import csv
from io import StringIO

from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from documents.serializers import DocumentSerializer
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from integrations.hotelbook_content import hotel_content


def content_response(request, supplier_id, snapshot):
    if snapshot.get("provider_adapter") != "hotelbook" or not snapshot.get("hotelbook", {}).get("hotel_id"):
        raise ApiError(
            code="HOTEL_CONTENT_UNAVAILABLE",
            message="У поставщика нет каталога номеров для этой услуги",
            status_code=404,
        )
    try:
        data = hotel_content(
            get_adapter("hotelbook"),
            AdapterContext(tenant_id=request.user.tenant_id, supplier_id=supplier_id),
            snapshot,
        )
    except AdapterError as exc:
        raise ApiError(code=exc.code, message=str(exc), status_code=502) from None
    if request.query_params.get("export") != "csv":
        return Response(data)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Отель", "ID номера", "Категория", "Описание", "Удобства", "Фотографии"])

    def cell(value):
        text = str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

    for room in data["rooms"]:
        writer.writerow(
            [
                cell(v)
                for v in (
                    data["hotel"]["name"],
                    room["id"],
                    room["name"],
                    room["description"],
                    "; ".join(room["amenities"]),
                    "\n".join(p["url"] for p in room["photos"]),
                )
            ]
        )
    response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="hotel-{data["hotel"]["id"]}-rooms.csv"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


class OfferHotelContentView(APIView):
    permission_classes = [require("services.search")]

    def get(self, request, offer_id):
        from orders.selectors import get_order_or_404
        from services.views import _get_offer

        offer = _get_offer(request, offer_id)
        if offer.session_id and offer.session.order_id:
            get_order_or_404(request.user, offer.session.order_id)
        return content_response(request, offer.supplier_id, offer.raw_snapshot or {})


class ServiceHotelContentView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, service_id):
        from services.views import _get_service

        service = _get_service(request, service_id)
        return content_response(request, service.supplier_id, service.provider_snapshot or {})


class HotelCertificateView(APIView):
    permission_classes = [require("orders.view"), require("documents.generate"), require("documents.view")]

    def post(self, request, service_id):
        from documents.hotel_certificate import ensure_certificate
        from services.views import _get_service

        service = _get_service(request, service_id)
        document = ensure_certificate(service, request.user)
        audit("documents.hotel_certificate_generated", actor=request.user, resource=document, request=request)
        return Response(DocumentSerializer(document).data)
