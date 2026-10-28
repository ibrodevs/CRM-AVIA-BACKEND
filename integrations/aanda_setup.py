"""Local A&A selection and storage checks. Never return secrets in diagnostics."""

from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Q

from suppliers.models import Supplier
from tenancy.models import Organization


def select_supplier(*, interactive=False):
    candidates = list(
        Supplier.objects.filter(archived_at__isnull=True, tenant__is_active=True)
        .filter(
            Q(automation_capabilities__adapter="aanda")
            | Q(credentials__provider_adapter="aanda", credentials__archived_at__isnull=True)
            | Q(name__icontains="a&a")
            | Q(name__icontains="roomlink")
        )
        .select_related("tenant")
        .distinct()
        .order_by("tenant__name", "name", "id")
    )
    if len(candidates) == 1:
        return candidates[0], candidates[0].tenant
    if candidates:
        if not interactive:
            raise CommandError("Несколько A&A suppliers: запустите configure_aanda для выбора")
        for index, supplier in enumerate(candidates, 1):
            print(f"{index}. {supplier.name} — {supplier.tenant.name}")
        return _choose(candidates, "Поставщик: "), None
    organizations = list(Organization.objects.filter(is_active=True).order_by("name", "id"))
    if not organizations:
        raise CommandError("Нет активной организации CRM")
    if len(organizations) == 1:
        return None, organizations[0]
    if not interactive:
        raise CommandError("Нет A&A supplier; несколько организаций: запустите configure_aanda")
    for index, organization in enumerate(organizations, 1):
        print(f"{index}. {organization.name} ({organization.slug})")
    return None, _choose(organizations, "Организация: ")


def _choose(items, prompt):
    try:
        index = int(input(prompt))
        if not 1 <= index <= len(items):
            raise ValueError
        return items[index - 1]
    except ValueError:
        raise CommandError("Некорректный номер") from None


def check_encrypted_storage(credential, config):
    with connection.cursor() as cursor:
        cursor.execute("SELECT encrypted_secrets FROM suppliers_credential WHERE id=%s", [credential.pk])
        stored = cursor.fetchone()[0]
    # A valid field round trip, plus the raw DB prefix, proves encrypted storage.
    if not stored.startswith("enc$1$") or config["password"] in stored:
        raise CommandError("Проверка шифрования credentials не пройдена")
