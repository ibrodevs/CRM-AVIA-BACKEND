"""Staged certification against the existing adapter, services and booking worker."""

from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.permissions import has_permission
from booking.models import BookingWorkflow, BookingWorkflowItem
from booking.preflight import run_preflight
from common.jobs import enqueue
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from integrations.certification import export_runs, summary
from integrations.models import HotelbookCertificationRun
from orders.selectors import get_order_or_404
from services.models import OrderService, ServicePassenger
from tenancy.context import tenant_context


class Command(BaseCommand):
    help = "HBPRO certification: prepare/book/refresh/cancel/export; mutations require explicit confirmation."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["prepare", "book", "refresh", "cancel", "export"])
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--actor", required=True, help="Authorized CRM user UUID")
        parser.add_argument("--supplier")
        parser.add_argument("--scenario", type=int, choices=[1, 2])
        parser.add_argument("--check-in")
        parser.add_argument(
            "--hotel-id", type=int, default=1251201, help="Test hotel ID for prepare (default: 1251201)"
        )
        parser.add_argument("--citizenship", default="KG")
        parser.add_argument("--run", action="append", default=[])
        parser.add_argument("--order")
        parser.add_argument(
            "--participants", nargs="+", help="Room order: male female male female for scenario 2"
        )
        parser.add_argument("--confirm-financial-actions", action="store_true")
        parser.add_argument("--output", default="hbpro-certification.zip")

    def handle(self, *args, **options):
        actor = User.objects.filter(pk=options["actor"], tenant_id=options["tenant"], status="active").first()
        if actor is None or not has_permission(actor, "integrations.manage"):
            raise CommandError("Authorized integrations.manage actor required")
        with tenant_context(actor.tenant_id):
            try:
                self.run_action(actor, options)
            except AdapterError as exc:
                raise CommandError(f"HBPRO {exc.code}: {exc}") from None

    def run_action(self, actor, options):
        action = options["action"]
        adapter = get_adapter("hotelbook")
        if action == "prepare":
            from suppliers.models import Supplier

            if not options["supplier"] or not options["scenario"] or not options["check_in"]:
                raise CommandError("prepare requires --supplier --scenario --check-in")
            if settings.HBPRO_ENVIRONMENT != "sandbox":
                raise CommandError("Certification requires HBPRO_ENVIRONMENT=sandbox")
            supplier = Supplier.objects.filter(
                pk=options["supplier"], tenant_id=actor.tenant_id, archived_at__isnull=True
            ).first()
            if not supplier:
                raise CommandError("Supplier not found")
            try:
                check_in = date.fromisoformat(options["check_in"])
                if check_in <= timezone.localdate():
                    raise ValueError
            except ValueError:
                raise CommandError("Use a future ISO --check-in date") from None
            hotel_id = options["hotel_id"]
            if hotel_id <= 0:
                raise CommandError("--hotel-id must be a positive integer")
            scenario = options["scenario"]
            run = HotelbookCertificationRun.objects.create(
                tenant_id=actor.tenant_id, supplier=supplier, scenario=scenario
            )
            ctx = AdapterContext(
                tenant_id=actor.tenant_id,
                supplier_id=supplier.id,
                certification_run_id=run.id,
                correlation_id=str(run.id),
            )
            criteria = {
                "location": str(hotel_id),
                "check_in": check_in.isoformat(),
                "check_out": (check_in + timedelta(days=3 if scenario == 1 else 2)).isoformat(),
                "rooms": 1 if scenario == 1 else 2,
                "guests": 1 if scenario == 1 else 4,
                "citizenship": options["citizenship"],
                "hotelbook": {"confirmationMode": "ON_REQUEST" if scenario == 1 else "ONLINE"},
            }
            # Small page size exercises limit/offset as requested in the certification sheet.
            adapter = type(adapter)()
            adapter.page_size = 1
            offers = adapter.search(ctx, "hotel", criteria)
            mode = criteria["hotelbook"]["confirmationMode"]
            offers = [o for o in offers if o["hotelbook"]["offer"].get("confirmationMode") == mode]
            offers = [
                o
                for o in offers
                if o["hotelbook"]["offer"].get("hotelId") == hotel_id
                and o["hotelbook"]["offer"].get("checkIn") == criteria["check_in"]
                and o["hotelbook"]["offer"].get("checkOut") == criteria["check_out"]
                and sum(r.get("quantity", 1) for r in o["hotelbook"]["offer"]["rooms"]) == criteria["rooms"]
                and all(
                    r.get("adults") == (1 if scenario == 1 else 2) and not r.get("children")
                    for r in o["hotelbook"]["offer"]["rooms"]
                )
            ]
            if not offers:
                raise CommandError(f"run={run.id}: no {mode} offers; rerun prepare with different dates")
            run.snapshot = adapter.revalidate(ctx, offers[0])["snapshot"]
            run.snapshot["certification_run_id"] = str(run.id)
            run.save(update_fields=["snapshot", "updated_at"])
            self.stdout.write(
                f"run={run.id} scenario={scenario} offers={len(offers)} selected={run.snapshot['hotelbook']['offer_id']} (booking NOT sent)"
            )
            return
        runs = list(
            HotelbookCertificationRun.objects.filter(
                tenant_id=actor.tenant_id, pk__in=options["run"]
            ).select_related("service", "supplier", "workflow")
        )
        if not runs or len(runs) != len(set(options["run"])):
            raise CommandError("Specify existing --run UUID(s) in this tenant")
        if action == "export":
            try:
                content = export_runs(runs)
            except ValueError as exc:
                raise CommandError(str(exc)) from None
            destination = Path(options["output"]).resolve()
            if destination.exists():
                raise CommandError("Output already exists; choose a new --output")
            destination.write_bytes(content)
            destination.chmod(0o600)
            self.stdout.write(str(destination))
            return
        if len(runs) != 1:
            raise CommandError("This action requires one --run")
        run = runs[0]
        if action in ("book", "cancel") and not options["confirm_financial_actions"]:
            raise CommandError("Financial action requires --confirm-financial-actions after owner's approval")
        if action == "book":
            if not options["order"] or not options["participants"]:
                raise CommandError("book requires --order and --participants")
            if settings.HBPRO_ENVIRONMENT != "sandbox" or run.environment != "sandbox":
                raise CommandError("Certification bookings require sandbox")
            order = get_order_or_404(actor, options["order"])
            people = {
                str(p.id): p for p in order.participants.filter(status="active").select_related("person")
            }
            selected = options["participants"]
            if (
                len(set(selected)) != len(selected)
                or len(selected) != (1 if run.scenario == 1 else 4)
                or any(p not in people for p in selected)
            ):
                raise CommandError("Provide exactly 1 / 4 distinct active order participants")
            if run.scenario == 2:
                genders = [
                    (
                        people[p].person.gender
                        if people[p].person
                        else (people[p].guest_snapshot or {}).get("gender")
                    )
                    for p in selected
                ]
                if any(set(genders[i : i + 2]) not in ({"male", "female"}, {"M", "F"}) for i in (0, 2)):
                    raise CommandError("Each room requires adults of different genders, in room order")
            with transaction.atomic():
                locked = HotelbookCertificationRun.objects.select_for_update().get(pk=run.id)
                if locked.service_id:
                    raise CommandError("Run already has a booking service; use refresh")
                service = OrderService.objects.create(
                    tenant_id=actor.tenant_id,
                    order=order,
                    kind="hotel",
                    supplier=run.supplier,
                    title=f"HBPRO certification {run.scenario}",
                    source="api",
                    provider_snapshot=run.snapshot,
                    currency=run.snapshot["price"]["currency"],
                    supplier_cost=run.snapshot["price"]["amount"],
                    client_total=run.snapshot["price"]["amount"],
                    created_by=actor,
                )
                for index, p in enumerate(selected):
                    ServicePassenger.objects.create(
                        tenant_id=actor.tenant_id,
                        service=service,
                        participant=people[p],
                        room_ref=f"room:{index // 2 if run.scenario == 2 else 0}",
                    )
                workflow = BookingWorkflow.objects.create(
                    tenant_id=actor.tenant_id, order=order, created_by=actor
                )
                BookingWorkflowItem.objects.create(
                    tenant_id=actor.tenant_id, workflow=workflow, service=service
                )
                result = run_preflight(workflow, actor)
                if (
                    not result["ok"]
                    or result.get("price_changes")
                    or any(w.get("code") == "RATE_CHANGED" for w in result.get("warnings", []))
                ):
                    raise CommandError(
                        "Preflight blocked or offer changed; prepare a fresh run and inspect CRM data"
                    )
                # Validate guests against every room before enqueueing a financial operation.
                from booking.job_handlers import _booking_passengers

                ctx = AdapterContext(
                    tenant_id=actor.tenant_id, supplier_id=run.supplier_id, certification_run_id=run.id
                )
                _, config = adapter._config(ctx)
                adapter._guests(ctx, config, service.provider_snapshot, _booking_passengers(service))
                workflow.status = "running"
                workflow.save(update_fields=["status"])
                job = enqueue(
                    "booking.run",
                    {"workflow_id": str(workflow.id)},
                    tenant_id=actor.tenant_id,
                    initiated_by=actor,
                    correlation_id=str(run.id),
                )
                workflow.job = job
                workflow.save(update_fields=["job"])
                locked.service, locked.workflow = service, workflow
                locked.save(update_fields=["service", "workflow", "updated_at"])
            self.stdout.write(f"job={job.id} workflow={workflow.id}; worker performs ONE supplier booking")
            return
        if not run.service_id:
            raise CommandError("Run has no service; use book first")
        item = run.workflow.items.get(service=run.service)
        if action == "refresh":
            ctx = AdapterContext(
                tenant_id=actor.tenant_id,
                supplier_id=run.supplier_id,
                order_id=run.service.order_id,
                certification_run_id=run.id,
                correlation_id=str(run.id),
            )
            _, config = adapter._config(ctx)
            booking = (run.service.provider_snapshot or {}).get("hotelbook_booking", {})
            # Official OpenAPI date filters, plus client ref to avoid unrelated orders.
            created = (booking.get("created_at") or run.created_at.isoformat())[:10]
            adapter._request(
                ctx,
                "search_orders",
                "GET",
                adapter._path(config, "order/gateway/orders")
                + "?"
                + urlencode(
                    {
                        "clientOrderId": booking.get("client_request_id", ""),
                        "orderCreatedDate[from]": created,
                        "orderCreatedDate[to]": created,
                        "pagination": "false",
                    }
                ),
            )
            job = enqueue(
                "booking.status_inquiry",
                {"item_id": str(item.id)},
                tenant_id=actor.tenant_id,
                initiated_by=actor,
                correlation_id=str(run.id),
            )
        else:
            if not has_permission(actor, "services.cancel"):
                raise CommandError("services.cancel required")
            if item.status != "booked":
                raise CommandError("Refresh and wait for confirmed booking before certification cancellation")
            job = enqueue(
                "booking.compensate",
                {"workflow_id": str(run.workflow_id), "service_ids": [str(run.service_id)]},
                tenant_id=actor.tenant_id,
                initiated_by=actor,
                correlation_id=str(run.id),
            )
        self.stdout.write(f"job={job.id}")
        self.stdout.write(str(summary(run)))
