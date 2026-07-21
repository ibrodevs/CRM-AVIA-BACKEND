import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.db import connection
from django.utils import timezone

from common.jobs import job_handler
from common.models import BackgroundJob
from common.outbox import emit_event
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from services.models import SearchProviderRun, SearchSession, ServiceOffer
from services.pricing import calculate_price, resolve_markup_rules
from suppliers.models import Supplier, SupplierSearchPriority


def _dedup_hash(kind: str, itinerary: dict, price_amount: str, currency: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "itinerary": itinerary, "price": price_amount, "currency": currency},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def _suppliers_for_search(session: SearchSession) -> list[Supplier | None]:
    priority = SupplierSearchPriority.objects.filter(
        tenant_id=session.tenant_id,
        service_kind=session.kind,
        is_active=True,
        archived_at__isnull=True,
    ).first()
    if priority and priority.ordered_suppliers:
        suppliers = list(
            Supplier.objects.filter(pk__in=priority.ordered_suppliers, status=Supplier.Status.ACTIVE)
        )
        by_id = {str(s.id): s for s in suppliers}
        ordered = [by_id[sid] for sid in map(str, priority.ordered_suppliers) if sid in by_id]
        if ordered:
            return ordered

    base_qs = Supplier.objects.filter(
        tenant_id=session.tenant_id,
        status=Supplier.Status.ACTIVE,
        archived_at__isnull=True,
    )
    if connection.vendor == "sqlite":
        suppliers = [
            supplier
            for supplier in base_qs
            if session.kind in (supplier.service_kinds or [])
        ]
    else:
        suppliers = list(base_qs.filter(service_kinds__contains=[session.kind]))
    return suppliers or [None]


@job_handler("services.search", user_cancellable=True)
def run_search(job: BackgroundJob) -> dict:
    session = SearchSession.objects.get(pk=job.payload["session_id"])
    if session.status == SearchSession.Status.CANCELLED:
        return {"status": "cancelled"}
    session.status = SearchSession.Status.RUNNING
    session.save(update_fields=["status"])

    suppliers = _suppliers_for_search(session)
    succeeded = failed = total_offers = 0
    seen_hashes: set[str] = set()

    for supplier in suppliers:
        adapter_key = "mock"
        if supplier is not None:
            credential = supplier.credentials.filter(archived_at__isnull=True, status="active").first()
            if credential is not None:
                adapter_key = credential.provider_adapter
        run = SearchProviderRun.objects.create(
            tenant_id=session.tenant_id,
            session=session,
            supplier=supplier,
            provider_adapter=adapter_key,
            status=SearchProviderRun.Status.RUNNING,
            started_at=timezone.now(),
        )
        ctx = AdapterContext(
            tenant_id=session.tenant_id,
            supplier_id=supplier.id if supplier else None,
            correlation_id=job.correlation_id or str(job.id),
        )
        try:
            if adapter_key == "mock" and not settings.ALLOW_MOCK_ADAPTER:
                raise AdapterError(
                    "PROVIDER_NOT_CONFIGURED",
                    "Sandbox adapter is disabled; configure a production provider adapter",
                    category="configuration",
                )
            adapter = get_adapter(adapter_key)
            raw_offers = adapter.search(ctx, session.kind, session.criteria)
        except AdapterError as exc:
            run.status = (
                SearchProviderRun.Status.TIMEOUT
                if exc.category == "timeout"
                else SearchProviderRun.Status.FAILED
            )
            run.error_code = exc.code
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "error_code", "completed_at"])
            failed += 1
            emit_event(
                "search.progress",
                session,
                payload={
                    "supplier": str(supplier.id) if supplier else None,
                    "status": "failed",
                    "error_code": exc.code,
                },
                audience_user=session.user,
            )
            continue

        created = 0
        for raw in raw_offers:
            price = raw.get("price", {})
            base = Decimal(str(price.get("amount", "0")))
            currency = price.get("currency", "USD")
            dedup = _dedup_hash(session.kind, raw.get("itinerary", {}), str(base), currency)
            if dedup in seen_hashes:
                continue
            seen_hashes.add(dedup)

            markup_rules = resolve_markup_rules(
                supplier,
                kind=session.kind,
                route=_criteria_route(session.criteria),
                cabin=str(session.criteria.get("cabin", "")),
            )
            offer = ServiceOffer.objects.create(
                tenant_id=session.tenant_id,
                session=session,
                kind=session.kind,
                supplier=supplier,
                provider_adapter=adapter_key,
                external_key=raw.get("external_key", ""),
                itinerary=raw.get("itinerary", {}),
                fare=raw.get("fare"),
                price_amount=base,
                price_currency=currency,
                availability=raw.get("availability", ""),
                expires_at=raw.get("expires_at"),
                raw_snapshot=raw,
                dedup_hash=dedup,
            )
            pricing = calculate_price(
                base=base,
                currency=currency,
                markup_rules=markup_rules,
                tenant_id=session.tenant_id,
                offer=offer,
                step="search",
            )
            offer.price_amount = pricing["total"]
            offer.applied_markup_rules = [c for c in pricing["components"] if c["name"] == "supplier_markup"]
            offer.save(update_fields=["price_amount", "applied_markup_rules"])
            created += 1

        run.status = SearchProviderRun.Status.SUCCEEDED
        run.offers_count = created
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "offers_count", "completed_at"])
        succeeded += 1
        total_offers += created
        emit_event(
            "search.progress",
            session,
            payload={
                "supplier": str(supplier.id) if supplier else None,
                "status": "succeeded",
                "offers": created,
            },
            audience_user=session.user,
        )

    session.refresh_from_db()
    if session.status == SearchSession.Status.CANCELLED:
        return {"status": "cancelled"}
    if succeeded == 0:
        session.status = SearchSession.Status.FAILED
    elif failed:
        session.status = SearchSession.Status.PARTIAL
    else:
        session.status = SearchSession.Status.COMPLETED
    session.expires_at = timezone.now() + timezone.timedelta(minutes=30)
    session.save(update_fields=["status", "expires_at"])
    emit_event(
        "search.completed" if succeeded else "search.failed",
        session,
        payload={"offers": total_offers, "failed_providers": failed},
        audience_user=session.user,
    )
    return {"offers": total_offers, "succeeded": succeeded, "failed": failed}


def _criteria_route(criteria: dict) -> str:
    origin = str(criteria.get("origin", "")).upper()
    destination = str(criteria.get("destination", "")).upper()
    return f"{origin}-{destination}" if origin and destination else ""
