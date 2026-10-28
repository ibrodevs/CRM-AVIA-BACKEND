"""Read-only live smoke check. Never sends OrderRequest or cancellation."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone

from integrations.aanda import AandaAdapter
from integrations.aanda_setup import check_encrypted_storage, select_supplier
from integrations.adapters import AdapterContext, AdapterError
from integrations.models import IntegrationLog
from suppliers.models import Supplier


class Command(BaseCommand):
    help = "A&A sandbox: проверка credentials, Search и Pricing (без booking/cancel)"

    def add_arguments(self, parser):
        parser.add_argument("--supplier", type=str)
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--city-code")
        group.add_argument("--hotel-code")
        parser.add_argument("--check-in")
        parser.add_argument("--check-out")

    def handle(self, *args, **options):
        try:
            with override_settings(DEBUG=False, AANDA_ENVIRONMENT="sandbox"):
                self.run_check(options)
        except CommandError:
            raise
        except Exception:
            raise CommandError("A&A check failed: внутренняя ошибка (детали скрыты)") from None

    @staticmethod
    def safe_error(exc):
        return (
            f"HTTP status: {getattr(exc, 'http_status', None) or 'N/A'}; "
            f"A&A error code: {getattr(exc, 'provider_code', '') or 'N/A'}; "
            f"Description: {exc.code}"
        )

    def run_check(self, options):
        adapter = AandaAdapter()
        supplier = (
            Supplier.objects.filter(pk=options["supplier"], archived_at__isnull=True).first()
            if options["supplier"]
            else select_supplier()[0]
        )
        if supplier is None:
            raise CommandError("Поставщик не найден")
        credential = supplier.credentials.filter(
            provider_adapter="aanda", environment="sandbox", archived_at__isnull=True
        ).first()
        if credential is None:
            raise CommandError("Sandbox credentials A&A не настроены")
        config = adapter._secrets(credential)
        check_encrypted_storage(credential, config)
        started = timezone.now()
        # CurrencyListRequest only: no order creation, booking or cancellation.
        try:
            adapter.verify_credentials(credential)
        except AdapterError as exc:
            credential.status = "failed"
            credential.last_verified_at = timezone.now()
            credential.save(update_fields=["status", "last_verified_at"])
            raise CommandError("A&A AUTH: FAILED\n" + self.safe_error(exc)) from None
        credential.status = "active"
        credential.last_verified_at = timezone.now()
        credential.save(update_fields=["status", "last_verified_at"])
        ctx = AdapterContext(tenant_id=supplier.tenant_id, supplier_id=supplier.id, environment="sandbox")
        selected, loaded = adapter._config(ctx)
        if selected.pk != credential.pk or loaded != config:
            raise CommandError("Sandbox credential selection check failed")
        logs = list(
            IntegrationLog.objects.filter(
                tenant_id=supplier.tenant_id,
                supplier_id=supplier.pk,
                provider_adapter="aanda",
                created_at__gte=started,
                operation="verify_credentials",
            ).values()
        )
        if not logs or any(
            row["environment"] != "sandbox"
            or row["url"] != "https://go-api.aanda.ru/"
            or row["request_sanitized"].get("request_name") != "CurrencyListRequest"
            or config["password"] in json.dumps(row, default=str)
            for row in logs
        ):
            raise CommandError("Sandbox / IntegrationLog safety check failed")
        self.stdout.write(
            "A&A AUTH: OK\nCredentials: OK\nSandbox connection: OK\n"
            "CurrencyListRequest: OK (read-only)\n"
            "Encrypted storage: OK\nIntegrationLog: OK (no password)"
        )
        if not options["city_code"] and not options["hotel_code"]:
            return
        start = timezone.localdate() + timedelta(days=30)
        criteria = {
            "check_in": options["check_in"] or start.isoformat(),
            "check_out": options["check_out"] or (start + timedelta(days=2)).isoformat(),
            "rooms": [{"adults": 1}],
            "aanda": {"hotel_codes": [options["hotel_code"]]}
            if options["hotel_code"]
            else {"city_code": options["city_code"]},
        }
        ctx = AdapterContext(tenant_id=supplier.tenant_id, supplier_id=supplier.id)
        try:
            offers = adapter.search(ctx, "hotel", criteria)
            self.stdout.write(f"A&A offers: {len(offers)}")
            offer = next((o for o in offers if o["availability"] in ("available", "on_request")), None)
            if offer:
                room = offer["aanda"]["rooms"][0]
                self.stdout.write(
                    f"hotel={room['hotel_code']} product={room['product_code']} rate={room['rate_id']} price={offer['price']['amount']} {offer['price']['currency']}"
                )
                result = adapter.revalidate(ctx, offer)
                self.stdout.write(f"Pricing: {result['status']}; terms_changed={result['terms_changed']}")
        except AdapterError as exc:
            raise CommandError("A&A read-only check failed: " + self.safe_error(exc)) from None
