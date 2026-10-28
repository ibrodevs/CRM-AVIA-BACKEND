"""Interactive local setup; credentials never enter argv/history or output."""

import getpass
import json
import sys
import warnings

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings

from integrations.aanda import AandaAdapter
from integrations.aanda_setup import check_encrypted_storage, select_supplier
from suppliers.models import Supplier, SupplierCredential


class Command(BaseCommand):
    help = "Безопасно сохранить и проверить sandbox credentials A&A"

    def handle(self, *args, **options):
        if not sys.stdin.isatty():
            raise CommandError("Запустите команду в интерактивном терминале")
        try:
            with override_settings(DEBUG=False, AANDA_ENVIRONMENT="sandbox"):
                self.configure()
        except (EOFError, KeyboardInterrupt):
            raise CommandError("Настройка прервана") from None
        except CommandError:
            raise
        except Exception:
            # Database/transport errors must never dump credential-bearing locals/SQL.
            raise CommandError("Настройка A&A не завершена; внутренняя ошибка (детали скрыты)") from None

    def configure(self):
        supplier, organization = select_supplier(interactive=True)
        if supplier:
            organization = supplier.tenant
        self.stdout.write("A&A Sandbox credentials\n")
        buyer_id = input("BuyerID: ").strip()
        user_id = input("Login / UserID: ").strip()
        with warnings.catch_warnings():
            # Fail closed if getpass cannot disable terminal echo.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password: ")
        if not all((buyer_id, user_id, password)):
            raise CommandError("BuyerID, Login / UserID и Password обязательны")
        with transaction.atomic():
            # Serialize local setup for this tenant, including first supplier creation.
            type(organization).objects.select_for_update().get(pk=organization.pk)
            if supplier is None:
                supplier, _ = Supplier.objects.get_or_create(
                    tenant=organization,
                    name="A&A / Roomlink",
                    archived_at__isnull=True,
                    defaults={
                        "status": "active",
                        "service_kinds": ["hotel"],
                        "automation_capabilities": {"adapter": "aanda"},
                    },
                )
            credential, created = SupplierCredential.objects.update_or_create(
                supplier=supplier,
                provider_adapter="aanda",
                environment="sandbox",
                archived_at__isnull=True,
                defaults={
                    "tenant": organization,
                    "encrypted_secrets": json.dumps(
                        {"buyer_id": buyer_id, "user_id": user_id, "password": password}
                    ),
                    "status": "inactive",
                    "last_verified_at": None,
                },
            )
            credential.refresh_from_db()
            config = AandaAdapter()._secrets(credential)
            if config != {"buyer_id": buyer_id, "user_id": user_id, "password": password}:
                raise CommandError("Проверка чтения credentials не пройдена")
            check_encrypted_storage(credential, config)
        self.stdout.write(
            "A&A credentials configured successfully.\n\n"
            f"Supplier: {supplier.name}\nAdapter: aanda\nEnvironment: sandbox\n"
            f"BuyerID: {buyer_id}\nUserID: {user_id}\nPassword: ********\n"
            f"Credential status: configured\nCredential: {credential.pk} "
            f"({'created' if created else 'updated'})\n"
        )
        call_command("aanda_sandbox_check", supplier=str(supplier.pk), stdout=self.stdout, stderr=self.stderr)
