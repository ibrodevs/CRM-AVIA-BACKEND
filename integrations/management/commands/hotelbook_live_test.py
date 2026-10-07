"""Read-only HB diagnostic: never create orders, book, retrieve or cancel items."""

from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.adapters import AdapterContext, AdapterError, get_adapter
from suppliers.models import Supplier
from tenancy.models import Organization


class Command(BaseCommand):
    help = "Local Hotelbook login/search/results/revalidate; no reservation mutations."

    def add_arguments(self, parser):
        parser.add_argument("--hotel-id", type=int, default=1251539)
        parser.add_argument("--check-in")
        parser.add_argument("--check-out")
        parser.add_argument("--citizenship")
        parser.add_argument("--tenant-id")
        parser.add_argument("--supplier-id")

    def handle(self, *args, **options):
        if settings.SETTINGS_MODULE not in ("config.settings.dev", "config.settings.test"):
            raise CommandError("Use config.settings.dev or config.settings.test for this diagnostic.")
        check_in = options["check_in"] or str(timezone.localdate() + timedelta(days=14))
        try:
            check_out = options["check_out"] or str(date.fromisoformat(check_in) + timedelta(days=2))
        except ValueError:
            raise CommandError("--check-in must use YYYY-MM-DD") from None
        # All temporary CRM rows and integration logs are rolled back even on failure.
        # Actual HB search remains read-only, with normal adapter token caching.
        with transaction.atomic():
            tenant = (
                Organization.objects.filter(pk=options["tenant_id"]).first()
                if options["tenant_id"]
                else Organization.objects.order_by("id").first()
            )
            if tenant is None:
                raise CommandError("No tenant found; bootstrap a local tenant first.")
            if options["supplier_id"]:
                supplier = Supplier.objects.filter(
                    pk=options["supplier_id"], tenant=tenant, archived_at__isnull=True
                ).first()
                if supplier is None:
                    raise CommandError("Supplier not found in selected tenant.")
            else:
                supplier = Supplier.objects.create(
                    tenant=tenant, name="HB Pro local diagnostic", service_kinds=["hotel"]
                )
            ctx = AdapterContext(tenant_id=tenant.id, supplier_id=supplier.id)
            adapter = get_adapter("hotelbook")
            try:
                credential, config = adapter._config(ctx)
                self.stdout.write(
                    "credentials_source="
                    + ("local_env" if credential._state.adding else "SupplierCredential")
                )
                # Prove real login rather than accepting a cached JWT.
                cache.delete(adapter._cache_key(credential, config, "token"))
                adapter.verify_credentials(credential)
                self.stdout.write("login=OK")
                criteria = {
                    "location": str(options["hotel_id"]),
                    "check_in": check_in,
                    "check_out": check_out,
                    "rooms": 1,
                    "guests": 1,
                }
                if options["citizenship"]:
                    criteria["citizenship"] = options["citizenship"]
                self.stdout.write(f"dates={check_in}..{check_out} hotel_id={options['hotel_id']}")
                offers = adapter.search(ctx, "hotel", criteria)
                self.stdout.write(f"results_count={len(offers)}")
                if not offers:
                    raise CommandError("NO_AVAILABILITY: no offers for selected dates/composition.")
                if any(o["hotelbook"]["hotel_id"] != options["hotel_id"] for o in offers):
                    raise CommandError("Unexpected hotel ID in search results.")
                for o in offers[:3]:
                    self.stdout.write(
                        f"hotel={o['itinerary']['property_name']} room={o['itinerary']['room']} price={o['price']['amount']} {o['price']['currency']}"
                    )
                result = adapter.revalidate(ctx, offers[0])
                self.stdout.write(f"revalidate={result['status']} terms_changed={result['terms_changed']}")
                snapshot = result["snapshot"]
                old_terms = adapter._terms(offers[0]["hotelbook"]["offer"])
                new_terms = adapter._terms(snapshot["hotelbook"]["offer"])
                changed_fields = [key for key in old_terms if old_terms[key] != new_terms[key]]
                self.stdout.write("terms_changed_fields=" + ",".join(changed_fields))
                second = adapter.revalidate(ctx, snapshot)
                self.stdout.write(
                    f"repeat_revalidate={second['status']} terms_changed={second['terms_changed']}"
                )
                snapshot = second["snapshot"]
                _, config = adapter._config(ctx)
                citizenship = criteria.get("citizenship") or config.get("default_citizenship")
                # Synthetic adult validates only the local guest mapping; nothing sent to book.
                rooms = adapter._guests(
                    ctx,
                    config,
                    snapshot,
                    [
                        {
                            "latin_given_name": "LOCAL",
                            "latin_surname": "TEST",
                            "gender": "M",
                            "birth_date": "1990-01-01",
                            "citizenship": citizenship,
                        }
                    ],
                )
                self.stdout.write(
                    f"booking_guest_mapping=OK rooms={len(rooms)}; orderId/contactInfo/customer require a real CRM order; booking NOT sent"
                )
            except AdapterError as exc:
                # Avoid printing provider response bodies or arbitrary exception values.
                raise CommandError(f"Hotelbook {exc.code}: category={exc.category}") from None
            finally:
                transaction.set_rollback(True)
