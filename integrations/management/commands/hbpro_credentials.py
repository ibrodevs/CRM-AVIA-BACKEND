"""Import server secrets without passing them on a command line or printing them."""

import json
import os
from getpass import getpass

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from suppliers.job_handlers import verify_supplier_credentials
from suppliers.models import Supplier, SupplierCredential


class Command(BaseCommand):
    help = "Import HBPRO env or prompted credentials; verify by real login (no bookings)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--supplier", required=True)
        parser.add_argument("--prompt", action="store_true")

    def handle(self, *args, **options):
        supplier = Supplier.objects.filter(
            pk=options["supplier"], tenant_id=options["tenant"], archived_at__isnull=True
        ).first()
        if supplier is None:
            raise CommandError("Supplier not found in tenant")
        config = {
            "login": getpass("HBPRO login: ") if options["prompt"] else os.environ.get("HBPRO_LOGIN", ""),
            "password": getpass("HBPRO password: ")
            if options["prompt"]
            else os.environ.get("HBPRO_PASSWORD", ""),
            "locale": os.environ.get("HBPRO_LOCALE", "ru"),
            "default_citizenship": os.environ.get("HBPRO_DEFAULT_CITIZENSHIP", "KG"),
            "pay_form": os.environ.get("HBPRO_PAY_FORM", "CASHLESS"),
        }
        if not config["login"] or not config["password"]:
            raise CommandError("Set HBPRO_LOGIN/HBPRO_PASSWORD in server environment or use --prompt")
        SupplierCredential.objects.update_or_create(
            tenant_id=supplier.tenant_id,
            supplier=supplier,
            provider_adapter="hotelbook",
            environment=settings.HBPRO_ENVIRONMENT,
            archived_at__isnull=True,
            defaults={"encrypted_secrets": json.dumps(config), "status": "inactive"},
        )
        result = verify_supplier_credentials(supplier)
        self.stdout.write(f"environment={settings.HBPRO_ENVIRONMENT} status={result['status']}")
        if result["status"] != "connected":
            raise CommandError("Connection failed; inspect sanitized supplier journal")
