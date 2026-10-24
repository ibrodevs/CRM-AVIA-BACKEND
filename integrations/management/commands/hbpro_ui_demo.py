"""Isolated localhost UI demo. Never connects to HBPRO or performs real bookings."""

import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.http import HttpResponse
from django.urls import clear_url_caches, path
from django.utils import timezone

DEMO_EMAIL = "hotel-demo@local.test"
DEMO_PASSWORD = "Hotel-Demo-2026!"
DEMO_DB = "travelhub_hbpro_ui_demo"
PHOTO_URL = "http://127.0.0.1:8000/__hbpro_demo__/room.svg"


class DemoResponse:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.data).encode()


class DemoGateway:
    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        if url.path.endswith("/gateway/login"):
            return DemoResponse({"token": "local-demo-token-no-real-access"})
        if request.get_method() != "GET":
            raise URLError("Local demo: all supplier mutations are blocked")
        if "/dict/gateway/hotel/hotels/" in url.path:
            return DemoResponse(demo_hotel())
        if url.path.endswith("/dict/gateway/hotel/rooms"):
            return DemoResponse(
                [
                    {
                        "id": "demo-standard",
                        "hotelId": 131687,
                        "name": "Стандарт (DEMO)",
                        "description": "Демонстрационный номер. Реальное наличие не проверяется.",
                        "amenityIds": [1],
                        "images": [{"variants": {"primary": PHOTO_URL, "preview": PHOTO_URL}}],
                    },
                    {
                        "id": "demo-family",
                        "hotelId": 131687,
                        "name": "Семейный (DEMO)",
                        "description": "Вторая категория для проверки перечня номеров.",
                        "amenityIds": [],
                        "images": [],
                    },
                ]
            )
        raise URLError("Local demo: unsupported supplier request blocked")


def demo_hotel():
    return {
        "id": 131687,
        "name": "Мастер-отель Первомайская - DEMO",
        "cityId": 1,
        "details": {
            "address": "Демонстрационный адрес",
            "description": "Локальная демонстрация интерфейса. Это не данные гостиницы и не реальное бронирование.",
            "earliestCheckInTime": "14:00",
            "latestCheckOutTime": "12:00",
        },
        "photos": [
            {
                "photoUrl": PHOTO_URL,
                "thumbnailUrl": PHOTO_URL,
                "description": "Демо-изображение, не фотография гостиницы",
                "isMain": True,
            }
        ],
        "roomAmenities": [{"id": 1, "description": "Wi-Fi (DEMO)"}],
    }


def seed_demo():
    from accounts.management.commands.bootstrap_tenant import sync_system_roles
    from accounts.models import Role, User, UserRole
    from crm.models import Person
    from integrations.hotelbook import HotelbookAdapter
    from orders.models import Order, OrderParticipant
    from services.models import OrderService, ServicePassenger
    from suppliers.models import Supplier, SupplierCredential
    from tenancy.models import Organization

    tenant, _ = Organization.objects.get_or_create(slug="hbpro-ui-demo", defaults={"name": "HBPRO UI DEMO"})
    sync_system_roles(tenant)
    user = User.objects.filter(email=DEMO_EMAIL).first()
    if user is None:
        user = User.objects.create_user(
            email=DEMO_EMAIL, password=DEMO_PASSWORD, tenant=tenant, status="active"
        )
    UserRole.objects.get_or_create(user=user, role=Role.objects.get(tenant=tenant, code="admin"))
    person, _ = Person.objects.get_or_create(
        tenant=tenant,
        latin_surname="DEMO",
        defaults={
            "surname": "Демо",
            "given_name": "Гость",
            "latin_given_name": "GUEST",
            "gender": "male",
            "birth_date": "1990-01-01",
            "citizenship": "KG",
        },
    )
    supplier, _ = Supplier.objects.get_or_create(
        tenant=tenant, name="HBPRO LOCAL DEMO", defaults={"service_kinds": ["hotel"]}
    )
    SupplierCredential.objects.get_or_create(
        tenant=tenant,
        supplier=supplier,
        provider_adapter="hotelbook",
        environment="sandbox",
        defaults={
            "status": "active",
            "encrypted_secrets": json.dumps(
                {"login": "LOCAL-DEMO-NO-ACCESS", "password": "NOT-A-REAL-CREDENTIAL"}
            ),
        },
    )
    check_in = timezone.localdate() + timedelta(days=30)
    raw = {
        "offerId": "local-demo-offer",
        "hotelId": 131687,
        "checkIn": check_in.isoformat(),
        "checkOut": (check_in + timedelta(days=3)).isoformat(),
        "price": {"amount": 6056, "currency": "RUB"},
        "confirmationMode": "ONLINE",
        "roomGroupName": "Стандарт DEMO",
        "rooms": [
            {
                "hash": "local-demo-room",
                "roomInfoId": "demo-standard",
                "roomName": "Стандарт (DEMO)",
                "quantity": 1,
                "adults": 1,
                "children": [],
            }
        ],
        "meal": {"description": "Завтрак (DEMO)"},
        "finePolicies": {"cancel": []},
    }
    for index, state in enumerate(["CONFIRMED", "CANCELED", "CONFIRM_IN_PROGRESS"], 1):
        order, _ = Order.objects.get_or_create(
            tenant=tenant,
            number=f"HBPRO-DEMO-{index}",
            defaults={
                "client_person": person,
                "operator": user,
                "created_by": user,
                "planned_start": check_in,
                "planned_end": check_in + timedelta(days=3),
                "purpose": "Локальная демонстрация: без реальной брони",
            },
        )
        participant, _ = OrderParticipant.objects.get_or_create(tenant=tenant, order=order, person=person)
        snapshot = HotelbookAdapter()._normalize(
            deepcopy(raw),
            str(uuid4()),
            (timezone.now() + timedelta(hours=4)).isoformat(),
            demo_hotel(),
            {"citizenshipId": 77},
        )
        snapshot.update(
            local_demo=True,
            hotelbook_booking={
                "order_id": 9000000 + index,
                "locator": f"LOCAL-DEMO-{index}",
                "client_request_id": f"LOCAL-DEMO-{index}",
                "state": "complete",
                "updated_at": timezone.now().isoformat(),
                "items": [
                    {
                        "itemId": str(uuid4()),
                        "status": state,
                        "providerData": {"providerReferenceId": f"DEMO-NOT-REAL-{index}"},
                    }
                ],
            },
        )
        service, _ = OrderService.objects.get_or_create(
            tenant=tenant,
            order=order,
            title="Гостиница DEMO - " + state,
            defaults={
                "kind": "hotel",
                "supplier": supplier,
                "source": "api",
                "status": "cancelled"
                if state == "CANCELED"
                else "booked"
                if state == "CONFIRMED"
                else "approval",
                "currency": "RUB",
                "supplier_cost": 6056,
                "client_total": 6056,
                "provider_snapshot": snapshot,
                "created_by": user,
            },
        )
        ServicePassenger.objects.get_or_create(tenant=tenant, service=service, participant=participant)


def demo_photo(request):
    return HttpResponse(
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="360"><rect width="600" height="360" fill="#e5edf6"/><rect x="90" y="140" width="420" height="120" rx="20" fill="#7997bc"/><text x="300" y="65" text-anchor="middle" font-family="sans-serif" font-size="26" fill="#16324f">DEMO - NOT A HOTEL PHOTO</text><text x="300" y="320" text-anchor="middle" font-family="sans-serif" font-size="22">Local UI test image</text></svg>',
        content_type="image/svg+xml",
    )


class Command(BaseCommand):
    help = "Local HBPRO UI demo on 127.0.0.1:8000, isolated database and mocked supplier only."

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        if (
            not settings.DEBUG
            or settings.HBPRO_ENVIRONMENT != "sandbox"
            or str(db["NAME"]) != DEMO_DB
            or db.get("HOST") not in ("", "localhost", "127.0.0.1")
        ):
            raise CommandError(
                f"Demo requires DEBUG=True, sandbox and local DATABASE_URL=postgres:///{DEMO_DB}"
            )
        from config import urls

        urls.urlpatterns.insert(0, path("__hbpro_demo__/room.svg", demo_photo))
        clear_url_caches()
        seed_demo()
        self.stdout.write(
            f"Local demo only: {DEMO_EMAIL} / {DEMO_PASSWORD}\nOrders: HBPRO-DEMO-1 (confirmed), HBPRO-DEMO-2 (cancelled), HBPRO-DEMO-3 (pending).\nAll HBPRO traffic is intercepted; do not start jobs/scheduler in this demo database."
        )
        with patch("integrations.hotelbook.build_opener", return_value=DemoGateway()):
            call_command("runserver", "127.0.0.1:8000", use_reloader=False)
