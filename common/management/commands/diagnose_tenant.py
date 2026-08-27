from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Диагностика организаций, пользователей и доступности данных для API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default="admin@travelhub.local",
            help="Email пользователя для детальной проверки (по умолчанию: admin@travelhub.local)",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Автоматически привязать пользователя к организации travelhub и восстановить роли",
        )

    def handle(self, *args, **options):
        from accounts.models import Role, User, UserRole, UserSession
        from crm.models import ClientProfile, Company, Person
        from finance.models import FinancialObligation
        from orders.models import Order
        from services.models import OrderService
        from suppliers.models import Supplier
        from tenancy.models import Organization

        self.stdout.write(self.style.MIGRATE_HEADING("=== ДИАГНОСТИКА СРЕДЫ И БАЗЫ ДАННЫХ ==="))
        self.stdout.write(f"DJANGO_SETTINGS_MODULE: {getattr(settings, 'SETTINGS_MODULE', '(не указан)')}")
        db_conf = settings.DATABASES.get("default", {})
        self.stdout.write(f"DB Engine: {db_conf.get('ENGINE')}")
        self.stdout.write(f"DB Name: {db_conf.get('NAME')}")
        self.stdout.write(f"DEBUG: {settings.DEBUG}")

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("=== ОРГАНИЗАЦИИ В БАЗЕ ДАННЫХ ==="))
        orgs = list(Organization.objects.all())
        if not orgs:
            self.stdout.write(
                self.style.ERROR("В базе нет ни одной организации! Запустите seed_demo_data или bootstrap_tenant.")
            )
        for org in orgs:
            user_count = User.objects.filter(tenant=org).count()
            order_count = Order.objects.filter(tenant=org).count()
            person_count = Person.objects.filter(tenant=org).count()
            client_count = ClientProfile.objects.filter(tenant=org).count()
            company_count = Company.objects.filter(tenant=org).count()
            supplier_count = Supplier.objects.filter(tenant=org).count()
            service_count = OrderService.objects.filter(tenant=org).count()
            obligation_count = FinancialObligation.objects.filter(tenant=org).count()

            self.stdout.write(
                self.style.SUCCESS(f"• [ID: {org.id}] {org.name} (slug='{org.slug}')")
                + f"\n    Пользователей: {user_count} | Заказов: {order_count} | Клиентов: {client_count}"
                + f"\n    Физлиц: {person_count} | Компаний: {company_count} | Поставщиков: {supplier_count}"
                + f"\n    Услуг: {service_count} | Обязательств: {obligation_count}"
            )

        target_email = options["email"]
        self.stdout.write("\n" + self.style.MIGRATE_HEADING(f"=== ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ: {target_email} ==="))
        user = User.objects.filter(email__iexact=target_email).first()
        if not user:
            self.stdout.write(self.style.ERROR(f"Пользователь с email '{target_email}' НЕ найден в базе!"))
            return

        roles = list(Role.objects.filter(user_roles__user=user).values_list("code", flat=True))
        self.stdout.write(f"ID пользователя: {user.id}")
        self.stdout.write(f"Email: {user.email}")
        self.stdout.write(f"Статус: {user.status} (active={user.is_active})")
        self.stdout.write(f"is_staff: {user.is_staff}, is_superuser: {user.is_superuser}")
        self.stdout.write(
            f"Организация (tenant): ID={user.tenant_id} (slug='{user.tenant.slug if user.tenant else 'None'}')"
        )
        self.stdout.write(f"Роли: {', '.join(roles) if roles else 'НЕТ РОЛЕЙ'}")

        # Count objects in user's tenant
        if user.tenant:
            user_orders = Order.objects.filter(tenant=user.tenant).count()
            user_clients = ClientProfile.objects.filter(tenant=user.tenant).count()
            user_companies = Company.objects.filter(tenant=user.tenant).count()
            self.stdout.write(
                f"Данные, доступные через API для {user.email}:\n"
                f"  Заказы: {user_orders}\n"
                f"  Клиенты: {user_clients}\n"
                f"  Компании: {user_companies}"
            )
            if user_orders == 0 and user.tenant.slug != "travelhub":
                self.stdout.write(
                    self.style.WARNING(
                        f"ВНИМАНИЕ: Пользователь привязан к организации '{user.tenant.slug}', "
                        "в которой нет заказов. Демо-данные обычно создаются в организации 'travelhub'."
                    )
                )

        if options["fix"]:
            self.stdout.write("\n" + self.style.MIGRATE_HEADING("=== ИСПРАВЛЕНИЕ ПРИВЯЗКИ ПОЛЬЗОВАТЕЛЯ ==="))
            travelhub_org = Organization.objects.filter(slug="travelhub").first()
            if not travelhub_org:
                self.stdout.write(self.style.ERROR("Организация 'travelhub' не найдена. Запустите seed_demo_data."))
                return

            if user.tenant_id != travelhub_org.id:
                old_tenant_slug = user.tenant.slug if user.tenant else "none"
                user.tenant = travelhub_org
                UserRole.objects.filter(user=user).exclude(role__tenant=travelhub_org).delete()
                UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
                self.stdout.write(f"Организация пользователя изменена с '{old_tenant_slug}' на 'travelhub'.")

            user.status = User.Status.ACTIVE
            user.is_staff = True
            user.set_password("Demo-Pass-2026!")
            user.save()

            admin_role = Role.objects.filter(tenant=travelhub_org, code="admin").first()
            if admin_role:
                UserRole.objects.get_or_create(user=user, role=admin_role)
                self.stdout.write("Назначена роль admin в организации travelhub.")

            self.stdout.write(self.style.SUCCESS(f"Пользователь {target_email} успешно исправлен и готов к работе!"))

