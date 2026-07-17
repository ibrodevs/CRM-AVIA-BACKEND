from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

REPORT: dict = {"created": [], "skipped": [], "warnings": []}


def _get_or_create(model, defaults=None, report_key=None, **lookup):
    obj, created = model.objects.get_or_create(defaults=defaults or {}, **lookup)
    key = report_key or f"{model.__name__}:{lookup}"
    (REPORT["created"] if created else REPORT["skipped"]).append(key)
    return obj


class Command(BaseCommand):
    help = "Создаёт демонстрационные данные (идемпотентно). Не для production."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Разрешить запуск при DEBUG=False")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo_data запрещено запускать в production (DEBUG=False). "
                "Для стенда используйте --force."
            )
        from accounts.management.commands.bootstrap_tenant import sync_system_roles
        from accounts.models import Role, User, UserRole
        from crm.models import (
            Agreement,
            ClientProfile,
            Company,
            Contract,
            FeeRule,
            Person,
            PersonDocument,
            SettlementProfile,
        )
        from finance.models import FinancialObligation
        from orders.services import create_order
        from suppliers.models import Supplier, SupplierMarkupRule
        from tenancy.context import tenant_context
        from tenancy.models import Organization
        from travel_policy.models import TravelPolicy

        tenant = _get_or_create(
            Organization, slug="travelhub", defaults={"name": "Travel Hub"}, report_key="tenant"
        )
        sync_system_roles(tenant)

        with tenant_context(tenant.id):
            users = {}
            for email, role_code, first, last in [
                ("admin@travelhub.local", "admin", "Александр", "Админов"),
                ("operator@travelhub.local", "operator", "Ольга", "Операторова"),
                ("accountant@travelhub.local", "accountant", "Борис", "Бухгалтеров"),
                ("manager@travelhub.local", "manager", "Мария", "Менеджерова"),
            ]:
                user = User.objects.filter(email=email).first()
                if user is None:
                    user = User.objects.create_user(
                        email=email,
                        password="Demo-Pass-2026!",
                        tenant=tenant,
                        status=User.Status.ACTIVE,
                        first_name=first,
                        last_name=last,
                    )
                    REPORT["created"].append(f"user:{email}")
                else:
                    REPORT["skipped"].append(f"user:{email}")
                role = Role.objects.get(tenant=tenant, code=role_code)
                UserRole.objects.get_or_create(user=user, role=role)
                users[role_code] = user

            ivanov = _get_or_create(
                Person,
                tenant=tenant,
                surname="Иванов",
                given_name="Пётр",
                defaults={
                    "middle_name": "Сергеевич",
                    "latin_surname": "IVANOV",
                    "latin_given_name": "PETR",
                    "birth_date": "1988-04-12",
                    "gender": "male",
                    "citizenship": "KG",
                    "phone": "+996700111222",
                    "email": "p.ivanov@example.com",
                    "city": "Бишкек",
                    "created_by": users["admin"],
                },
                report_key="person:ivanov",
            )
            if not ivanov.documents.exists():
                PersonDocument.objects.create(
                    tenant=tenant,
                    person=ivanov,
                    type="foreign_passport",
                    number="AC3456789",
                    issuing_country="KG",
                    issued_at="2020-01-15",
                    expires_at="2030-01-15",
                    created_by=users["admin"],
                )
                REPORT["created"].append("document:ivanov")
            _get_or_create(
                ClientProfile,
                tenant=tenant,
                person=ivanov,
                defaults={
                    "status": "active",
                    "source": "phone",
                    "assigned_manager": users["manager"],
                    "created_by": users["admin"],
                },
                report_key="client:ivanov",
            )

            company = _get_or_create(
                Company,
                tenant=tenant,
                tax_id="01508201910114",
                defaults={
                    "legal_name": "ОсОО «Азия Трэвел Групп»",
                    "short_name": "АТГ",
                    "type": "llc",
                    "legal_address": "Бишкек, пр. Чуй 155",
                    "phone": "+996312900100",
                    "email": "office@atg.example.com",
                    "director": "Касымов Т.Б.",
                    "assigned_manager": users["manager"],
                    "created_by": users["admin"],
                },
                report_key="company:atg",
            )
            _get_or_create(
                SettlementProfile,
                company=company,
                defaults={
                    "tenant": tenant,
                    "mode": "deposit",
                    "currency": "USD",
                    "deposit_balance": Decimal("5000.00"),
                    "credit_limit": Decimal("10000.00"),
                },
                report_key="settlement:atg",
            )
            contract = _get_or_create(
                Contract,
                tenant=tenant,
                company=company,
                number="ДОГ-2026/014",
                defaults={
                    "status": "active",
                    "signed_at": "2026-01-10",
                    "starts_at": "2026-01-10",
                    "created_by": users["admin"],
                },
                report_key="contract:atg",
            )
            agreement = _get_or_create(
                Agreement,
                contract=contract,
                agreement_version=1,
                defaults={
                    "tenant": tenant,
                    "number": "СОГЛ-1",
                    "status": "active",
                    "effective_from": "2026-01-10",
                    "created_by": users["admin"],
                },
                report_key="agreement:atg",
            )
            if not agreement.fee_rules.exists():
                FeeRule.objects.create(
                    tenant=tenant,
                    agreement=agreement,
                    service_kind="avia",
                    fee_kind="service",
                    calculation="fixed",
                    value=Decimal("15"),
                    currency="USD",
                    description="Сервисный сбор за сегмент",
                    created_by=users["admin"],
                )
                REPORT["created"].append("fee_rule:atg")
            _get_or_create(
                TravelPolicy,
                tenant=tenant,
                company=company,
                name="Базовая политика",
                defaults={
                    "allowed_avia_cabins": ["economy"],
                    "price_limits": {"avia": {"amount": "800", "currency": "USD"}},
                    "created_by": users["admin"],
                },
                report_key="travel_policy:atg",
            )

            supplier = _get_or_create(
                Supplier,
                tenant=tenant,
                name="Demo GDS",
                defaults={
                    "status": "active",
                    "organization_type": "gds",
                    "service_kinds": ["avia", "rail", "hotel"],
                    "currencies": ["USD", "EUR"],
                    "created_by": users["admin"],
                },
                report_key="supplier:demo-gds",
            )
            _get_or_create(
                SupplierMarkupRule,
                tenant=tenant,
                supplier=supplier,
                service_kind="avia",
                amount_type="percent",
                defaults={"amount_value": Decimal("5"), "priority": 10, "created_by": users["admin"]},
                report_key="markup:demo-gds",
            )
            _get_or_create(
                Supplier,
                tenant=tenant,
                name="Гостиницы Иссык-Куля",
                defaults={
                    "status": "active",
                    "organization_type": "hotel_chain",
                    "service_kinds": ["hotel"],
                    "is_global": False,
                    "communication_methods": ["email", "phone"],
                    "created_by": users["admin"],
                },
                report_key="supplier:hotels",
            )

            from orders.models import Order

            if not Order.objects.filter(tenant=tenant).exists():
                order = create_order(
                    tenant_id=tenant.id,
                    user=users["operator"],
                    data={
                        "request_type": "individual",
                        "client_person": ivanov,
                        "purpose": "Отпуск в Стамбуле",
                        "planned_start": (timezone.now() + timedelta(days=30)).date(),
                        "planned_end": (timezone.now() + timedelta(days=37)).date(),
                        "base_currency": "USD",
                        "route": {
                            "kind": "round_trip",
                            "points": [
                                {"location_code": "FRU", "location_type": "airport"},
                                {"location_code": "IST", "location_type": "airport"},
                            ],
                        },
                        "participants": [{"person": ivanov, "role": "passenger", "is_contact": True}],
                    },
                )
                REPORT["created"].append(f"order:{order.number}")

                corporate = create_order(
                    tenant_id=tenant.id,
                    user=users["manager"],
                    data={
                        "request_type": "corporate",
                        "client_company": company,
                        "agreement": agreement,
                        "purpose": "Командировка отдела продаж",
                        "planned_start": (timezone.now() + timedelta(days=14)).date(),
                        "base_currency": "USD",
                    },
                )
                REPORT["created"].append(f"order:{corporate.number}")
                FinancialObligation.objects.create(
                    tenant=tenant,
                    order=corporate,
                    direction="client_receivable",
                    currency="USD",
                    original_amount=Decimal("1720.00"),
                    due_date=(timezone.now() + timedelta(days=7)).date(),
                    created_by=users["accountant"],
                )
                REPORT["created"].append("obligation:corporate")
            else:
                REPORT["skipped"].append("orders (уже существуют)")

        self.stdout.write(
            self.style.SUCCESS(
                f"Создано: {len(REPORT['created'])}, пропущено (идемпотентность): "
                f"{len(REPORT['skipped'])}, предупреждений: {len(REPORT['warnings'])}"
            )
        )
        for item in REPORT["created"]:
            self.stdout.write(f"  + {item}")
        if REPORT["warnings"]:
            for warning in REPORT["warnings"]:
                self.stdout.write(self.style.WARNING(f"  ! {warning}"))
        self.stdout.write("Демо-пароль пользователей: Demo-Pass-2026!")
