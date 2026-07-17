from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role, RolePermission, User, UserRole
from accounts.permissions_catalog import SYSTEM_ROLES
from tenancy.models import Organization


def sync_system_roles(tenant: Organization) -> None:
    for code, spec in SYSTEM_ROLES.items():
        role, _ = Role.objects.get_or_create(
            tenant=tenant,
            code=code,
            defaults={"name": spec["name"], "is_system": True},
        )
        existing = set(role.permissions.values_list("permission_code", flat=True))
        wanted = set(spec["permissions"])
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, permission_code=c) for c in wanted - existing]
        )
        role.permissions.filter(permission_code__in=existing - wanted).delete()


class Command(BaseCommand):
    help = "Создаёт организацию, системные роли и администратора (идемпотентно)"

    def add_arguments(self, parser):
        parser.add_argument("--org", default="Travel Hub")
        parser.add_argument("--slug", default="travelhub")
        parser.add_argument("--admin-email", default=None)
        parser.add_argument("--admin-password", default=None)

    @transaction.atomic
    def handle(self, *args, **options):
        tenant, created = Organization.objects.get_or_create(
            slug=options["slug"], defaults={"name": options["org"]}
        )
        self.stdout.write(f"Организация: {tenant.name} ({'создана' if created else 'существует'})")

        sync_system_roles(tenant)
        self.stdout.write(f"Системные роли синхронизированы: {', '.join(SYSTEM_ROLES)}")

        email = options["admin_email"]
        if email:
            password = options["admin_password"]
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                if not password:
                    raise CommandError("--admin-password обязателен при создании администратора")
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    tenant=tenant,
                    status=User.Status.ACTIVE,
                    is_staff=True,
                    first_name="Администратор",
                )
                self.stdout.write(f"Администратор создан: {email}")
            admin_role = Role.objects.get(tenant=tenant, code="admin")
            UserRole.objects.get_or_create(user=user, role=admin_role)
            self.stdout.write("Роль admin назначена")
