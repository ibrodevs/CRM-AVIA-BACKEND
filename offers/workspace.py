"""Interactive proposals: isolated drafts, versioned public terms and explicit consent.

This module never books, pays or issues a supplier service.
"""

import copy
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from crm.models import Company, Person
from offers.models import ProposalInteraction, ProposalWorkspace
from services.models import OrderService, ServiceOffer
from travel_policy.models import TravelPolicy

KINDS = {
    "avia",
    "hotel",
    "transfer",
    "rail",
    "insurance",
    "bus",
    "lounge",
    "aeroexpress",
    "tour",
    "visa",
    "other",
}
STAGES = {
    "draft": "Черновик",
    "published": "Выбор клиента",
    "review": "Проверка оператором",
    "awaiting_approval": "Ожидает согласования",
    "approved": "Согласовано",
}


def fail(message, code="VALIDATION_ERROR", status=400):
    raise ApiError(code=code, message=message, status_code=status)


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("99999999999.99"):
            raise ValueError
        return str(result.quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        fail("Укажите корректную неотрицательную стоимость каждой услуги")


def text(value, limit=4000):
    if not isinstance(value, str):
        fail("Текстовое поле имеет неверный формат")
    if len(value) > limit:
        fail(f"Максимальная длина текста — {limit} символов")
    return value.strip()


def all_options(payload):
    return {option["id"]: option for group in payload.get("groups", []) for option in group["options"]}


def auto_supported(row, tenant_id):
    from integrations.adapters import AdapterError, ProviderAdapter, adapter_for_service, get_adapter

    try:
        if row.get("offer"):
            source = ServiceOffer.objects.filter(pk=row["offer"], tenant_id=tenant_id).first()
            adapter = get_adapter(source.provider_adapter) if source and source.provider_adapter else None
        elif row.get("service"):
            source = OrderService.objects.filter(pk=row["service"], tenant_id=tenant_id, source="api").first()
            adapter = adapter_for_service(source) if source else None
        else:
            return False
        return bool(
            adapter
            and adapter.key not in {"mock", "base"}
            and type(adapter).revalidate is not ProviderAdapter.revalidate
        )
    except AdapterError:
        return False


def seed(proposal):
    groups, packages = [], []
    # Existing variants remain independent packages until an operator explicitly
    # permits mixing. Never infer supplier compatibility from the service kind.
    for variant in proposal.variants.prefetch_related("items").order_by("sequence"):
        ids = []
        for item in variant.items.all():
            key = str(item.pk)
            ids.append(key)
            groups.append(
                {
                    "id": f"g-{key}",
                    "title": item.title,
                    "required": False,
                    "options": [
                        {
                            "id": key,
                            "kind": item.service_kind or (item.offer.kind if item.offer_id else "other"),
                            "title": item.title,
                            "description": item.description,
                            "price": str(item.price_amount * item.quantity),
                            "currency": proposal.currency,
                            "availability": "unknown",
                            "checked_at": None,
                            "check_source": "manual",
                            "requires": [],
                            "excludes": [],
                            "offer": str(item.offer_id) if item.offer_id else None,
                            "service": str(item.service_id) if item.service_id else None,
                        }
                    ],
                }
            )
        if ids:
            packages.append(
                {"id": str(variant.pk), "title": variant.name, "option_ids": ids, "total_only": False}
            )
    return {
        "title": proposal.purpose or f"Предложение {proposal.number}",
        "intro": "",
        "recipient": proposal.recipient,
        "route": text(proposal.brief.get("route", ""), 500),
        "travel_start": "",
        "travel_end": "",
        "currency": proposal.currency,
        "mode": "packages" if packages else "flexible",
        "valid_until": proposal.valid_until.isoformat() if proposal.valid_until else None,
        "groups": groups,
        "packages": packages,
    }


def workspace_for(proposal):
    workspace, _ = ProposalWorkspace.objects.get_or_create(
        proposal=proposal, defaults={"draft": seed(proposal)}
    )
    return workspace


def normalize(raw, proposal, *, previous=None, allow_incomplete=False):
    if not isinstance(raw, dict):
        fail("Некорректная форма КП")
    currency = text(raw.get("currency", proposal.currency), 3).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        fail("Укажите валюту КП")
    mode = raw.get("mode", "flexible")
    if mode not in {"packages", "flexible", "both"}:
        fail("Неизвестный способ выбора услуг")
    valid_until = raw.get("valid_until") or None
    if valid_until and (not isinstance(valid_until, str) or parse_datetime(valid_until) is None):
        fail("Некорректный срок действия")
    if valid_until:
        value = parse_datetime(valid_until)
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        valid_until = value.isoformat()
    travel_dates = {}
    for key in ("travel_start", "travel_end"):
        value = raw.get(key) or ""
        if value and (not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
            fail("Укажите даты поездки в формате ГГГГ-ММ-ДД")
        try:
            parsed = parse_date(value) if value else None
        except ValueError:
            parsed = None
        if value and parsed is None:
            fail("Некорректная дата поездки")
        travel_dates[key] = value
    if travel_dates["travel_end"] and not travel_dates["travel_start"]:
        fail("Укажите начало поездки")
    if travel_dates["travel_end"] and travel_dates["travel_end"] < travel_dates["travel_start"]:
        fail("Окончание поездки не может быть раньше начала")
    payload = {
        **travel_dates,
        "recipient": text(raw.get("recipient", proposal.recipient), 255),
        "route": text(raw.get("route", ""), 500),
        "title": text(raw.get("title", ""), 255),
        "intro": text(raw.get("intro", "")),
        "currency": currency,
        "mode": mode,
        "valid_until": valid_until,
        "groups": [],
        "packages": [],
    }
    reference = raw.get("recipient_ref")
    if reference:
        if not isinstance(reference, dict) or reference.get("type") not in {"person", "company"}:
            fail("Некорректная привязка получателя")
        model = Person if reference["type"] == "person" else Company
        try:
            exists = model.objects.filter(pk=reference.get("id"), tenant_id=proposal.tenant_id).exists()
        except (ValidationError, ValueError, TypeError):
            exists = False
        if not exists:
            fail("Получатель недоступен в вашей организации")
        payload["recipient_ref"] = {"type": reference["type"], "id": str(reference["id"])}
    policy_id = raw.get("policy_id")
    if policy_id:
        company_id = proposal.order.client_company_id if proposal.order_id else None
        company_id = company_id or (
            reference.get("id") if reference and reference.get("type") == "company" else None
        )
        try:
            policy = (
                TravelPolicy.objects.filter(
                    pk=policy_id,
                    tenant_id=proposal.tenant_id,
                    company_id=company_id,
                    is_active=True,
                    archived_at__isnull=True,
                ).first()
                if company_id
                else None
            )
        except (ValidationError, ValueError, TypeError):
            policy = None
        if not policy:
            fail("Выберите действующую тревел-политику компании получателя")
        payload["policy_id"] = str(policy.pk)
        payload["policy_snapshot"] = {
            "name": policy.name,
            "version": policy.policy_version,
            "approver_chain": policy.approver_chain,
            "scopes": policy.scopes,
        }
    groups = raw.get("groups", [])
    packages = raw.get("packages", [])
    if (
        not isinstance(groups, list)
        or len(groups) > 50
        or not isinstance(packages, list)
        or len(packages) > 50
    ):
        fail("Допускается до 50 разделов и комплектов")
    previous_options = all_options(previous or {})
    group_ids, option_ids = set(), set()
    for group in groups:
        if not isinstance(group, dict):
            fail("Некорректный раздел КП")
        gid = text(group.get("id", ""), 80)
        if not gid or gid in group_ids:
            fail("Разделы должны иметь уникальные идентификаторы")
        group_ids.add(gid)
        options = group.get("options", [])
        if not isinstance(options, list) or len(options) > 50:
            fail("Допускается до 50 вариантов в разделе")
        normalized_group = {
            "id": gid,
            "title": text(group.get("title", ""), 255),
            "required": bool(group.get("required", False)),
            "kind": group.get("kind")
            if group.get("kind") in KINDS
            else next((o.get("kind") for o in options if o.get("kind") in KINDS), "other"),
            "options": [],
        }
        for option in options:
            if not isinstance(option, dict):
                fail("Некорректный вариант услуги")
            oid = text(option.get("id", ""), 80)
            if not oid or oid in option_ids:
                fail("Услуги должны иметь уникальные идентификаторы")
            option_ids.add(oid)
            if len(option_ids) > 200:
                fail("В КП допускается до 200 услуг")
            kind = option.get("kind", "other")
            if kind not in KINDS:
                fail("Неизвестный вид услуги")
            price = (
                "" if allow_incomplete and option.get("price") in (None, "") else amount(option.get("price"))
            )
            availability = option.get("availability", "unknown")
            if availability not in {"available", "unavailable", "unknown"}:
                fail("Неизвестный статус доступности")
            row = {
                "id": oid,
                "kind": kind,
                "title": text(option.get("title", ""), 255),
                "description": text(option.get("description", "")),
                "price": price,
                "currency": currency,
                "availability": availability,
                "checked_at": None,
                "check_source": "manual",
            }
            for relation in ("requires", "excludes"):
                refs = option.get(relation, [])
                if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                    fail("Некорректные ограничения сочетаний")
                row[relation] = list(dict.fromkeys(refs))
            # Supplier IDs are tenant-scoped and never exposed by the public API.
            for binding, model in (("offer", ServiceOffer), ("service", OrderService)):
                source_id = option.get(binding)
                if source_id:
                    try:
                        source = model.objects.filter(pk=source_id, tenant_id=proposal.tenant_id).first()
                    except (ValueError, TypeError, ValidationError):
                        fail("Некорректная ссылка на услугу")
                    if (
                        source is None
                        or source.kind != kind
                        or (binding == "service" and source.order_id != proposal.order_id)
                    ):
                        fail("Услуга недоступна в этом заказе")
                    row[binding] = str(source.pk)
                    baseline = source.price_amount if binding == "offer" else source.client_total
                    source_currency = source.price_currency if binding == "offer" else source.currency
                    if source_currency != currency:
                        fail("Валюты услуги и КП должны совпадать; автоматическая конвертация не выполняется")
                    old_binding = previous_options.get(oid, {})
                    if price == "":
                        continue
                    if (
                        old_binding.get(binding) == str(source.pk)
                        and old_binding.get("price_offset") is not None
                    ):
                        row["price_offset"] = str(
                            Decimal(old_binding["price_offset"])
                            + Decimal(price)
                            - Decimal(old_binding["price"])
                        )
                    else:
                        row["price_offset"] = str(Decimal(price) - baseline)
            old = previous_options.get(oid)
            if old and old.get("price") not in (None, ""):
                if old.get("price") != price:
                    row["previous_price"] = old["price"]
                elif old.get("previous_price") is not None:
                    row["previous_price"] = old["previous_price"]
            unchanged = old and all(
                old.get(key) == row.get(key)
                for key in ("price", "availability", "description", "title", "currency", "offer", "service")
            )
            if unchanged:
                row["checked_at"] = old.get("checked_at")
                row["check_source"] = old.get("check_source", "manual")
            if option.get("clear_check"):
                row["checked_at"] = None
            if option.get("mark_checked") and price != "":
                row["checked_at"] = timezone.now().isoformat()
                row["check_source"] = "manual"
            normalized_group["options"].append(row)
        payload["groups"].append(normalized_group)
    for row in all_options(payload).values():
        if any(ref not in option_ids or ref == row["id"] for ref in row["requires"] + row["excludes"]):
            fail("Ограничения должны ссылаться на другие услуги этого КП")
        if set(row["requires"]) & set(row["excludes"]):
            fail("Услуга не может одновременно требовать и исключать другой вариант")
    package_ids = set()
    for package in packages:
        if not isinstance(package, dict):
            fail("Некорректный комплект")
        key = text(package.get("id", ""), 80)
        ids = package.get("option_ids", [])
        if (
            not key
            or key in package_ids
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(ref, str) or ref not in option_ids for ref in ids)
        ):
            fail("Комплект должен содержать существующие услуги и уникальный идентификатор")
        package_ids.add(key)
        payload["packages"].append(
            {
                "id": key,
                "title": text(package.get("title", ""), 255),
                "option_ids": list(dict.fromkeys(ids)),
                "total_only": bool(package.get("total_only")),
            }
        )
    for row in all_options(payload).values():
        row["auto_check"] = auto_supported(row, proposal.tenant_id)
    return payload


def effective(workspace, publication):
    payload = copy.deepcopy(publication.payload)
    for oid, option in all_options(payload).items():
        check = workspace.checks.get(oid)
        if check:
            option.update(check)
    return payload


def validate_selection(payload, ids, package_id=None, *, checked=False):
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        fail("Выберите услуги без повторений")
    options = all_options(payload)
    chosen = set(ids)
    if any(oid not in options for oid in ids):
        fail("Выбранные услуги изменились; обновите предложение", "STALE_PROPOSAL", 409)
    package = next((p for p in payload["packages"] if p["id"] == package_id), None)
    if package_id and (package is None or set(package["option_ids"]) != chosen):
        fail("Состав комплекта изменён")
    if payload["mode"] == "packages" and not package:
        fail("Выберите один из готовых комплектов")
    # Packages are an explicitly operator-defined set. Flexible choices allow
    # at most one option from each group and respect required groups.
    if not package:
        for group in payload["groups"]:
            count = len(chosen & {o["id"] for o in group["options"]})
            if count > 1 or (group["required"] and count != 1):
                fail(f"Проверьте выбор в разделе «{group['title']}»")
    bindings = set()
    for oid in ids:
        row = options[oid]
        source = row.get("service") or row.get("offer")
        if source and source in bindings:
            fail("Одна и та же услуга поставщика выбрана дважды")
        if source:
            bindings.add(source)
        if not set(row["requires"]).issubset(chosen) or set(row["excludes"]) & chosen:
            fail(f"«{row['title']}»: недопустимое сочетание услуг")
        if row["availability"] == "unavailable":
            fail(f"«{row['title']}» больше недоступна", "UNAVAILABLE", 409)
        if checked:
            timestamp = parse_datetime(row.get("checked_at") or "")
            if (
                row["availability"] != "available"
                or not timestamp
                or (timezone.now() - timestamp).total_seconds() > 900
                or row.get("check_error")
            ):
                fail(
                    f"Перед согласованием перепроверьте «{row['title']}» (проверка действует 15 минут)",
                    "CHECK_REQUIRED",
                    409,
                )
    return str(sum((Decimal(options[oid]["price"]) for oid in ids), Decimal(0)))


def record(workspace, action, data=None, actor=None, *, notify=False):
    event = ProposalInteraction.objects.create(
        workspace=workspace, revision=workspace.revision, action=action, data=data or {}, actor=actor
    )
    proposal = workspace.proposal
    audit(
        f"offers.kp_{action}",
        actor=actor,
        resource=proposal.order or proposal,
        tenant_id=proposal.tenant_id,
        after={
            "proposal": str(proposal.pk),
            "number": proposal.number,
            "revision": workspace.revision,
            **(data or {}),
        },
    )
    emit_event(
        "offers.proposal_updated",
        proposal,
        tenant_id=proposal.tenant_id,
        payload={"action": action, "revision": workspace.revision},
    )
    if notify:
        from notifications.models import Notification

        recipient = (proposal.order.operator if proposal.order_id else None) or proposal.created_by
        if recipient:
            titles = {
                "selection": "Клиент выбрал услуги",
                "approval": "Клиент согласовал КП",
                "alternative": "Клиент запросил замену",
                "comment": "Комментарий к КП",
                "price_changed": "Изменились условия КП",
            }
            Notification.objects.create(
                tenant_id=proposal.tenant_id,
                user=recipient,
                source="offers",
                event_type=f"offers.kp_{action}",
                title=f"{titles.get(action, 'Обновление КП')} · {proposal.number}",
                body=str((data or {}).get("comment") or "Откройте КП и проверьте изменения"),
                resource_type="Proposal",
                resource_id=str(proposal.pk),
                deep_link="offers",
            )
    return event


def serialize(workspace, *, public=False, user=None):
    publication = workspace.publications.filter(revision=workspace.revision).first()
    payload = effective(workspace, publication) if publication else None
    if public:
        if not publication:
            fail("Предложение ещё не опубликовано", "NOT_FOUND", 404)
        payload.pop("recipient_ref", None)
        payload.pop("policy_id", None)
        payload.pop("policy_snapshot", None)
        # Explicit allowlist: no bindings, offsets, supplier snapshots or margins.
        for row in all_options(payload).values():
            row["auto_check"] = auto_supported(row, workspace.proposal.tenant_id)
            for key in list(row):
                if key not in {
                    "id",
                    "kind",
                    "title",
                    "description",
                    "price",
                    "currency",
                    "availability",
                    "checked_at",
                    "check_source",
                    "requires",
                    "excludes",
                    "previous_price",
                    "check_error",
                    "auto_check",
                }:
                    row.pop(key)
    result = {
        "version": workspace.version,
        "revision": workspace.revision,
        "stage": workspace.stage,
        "stage_label": STAGES[workspace.stage],
        "number": workspace.proposal.number,
        "payload": payload,
        "selection": workspace.selection,
        "events": [
            {
                "action": e.action,
                "data": e.data,
                "revision": e.revision,
                "actor": e.actor.get_full_name() if e.actor else "Клиент",
                "at": e.created_at.isoformat(),
            }
            for e in (
                workspace.interactions.exclude(action__startswith="internal_")
                if public
                else workspace.interactions
            )
            .select_related("actor")
            .order_by("-id")[:100]
        ],
    }
    if not public:
        from offers.internal_approval import summary as approval_summary

        result.update(
            internal_approval=approval_summary(workspace, user),
            internal_approval_history=[
                {"id": str(a.pk), "title": a.payload.get("title", ""), "at": a.created_at.isoformat(),
                 "status": a.status, "decisions": a.decisions, "fingerprint": a.fingerprint}
                for a in workspace.internal_approvals.order_by("-created_at")[:50]
            ],
            draft=workspace.draft,
            company_id=str(workspace.proposal.order.client_company_id)
            if workspace.proposal.order_id and workspace.proposal.order.client_company_id
            else None,
            draft_revisions=[
                {
                    "version": row.version,
                    "title": row.payload.get("title", ""),
                    "at": row.created_at.isoformat(),
                    "actor": row.created_by.get_full_name() if row.created_by else "Сотрудник",
                }
                for row in workspace.draft_revisions.select_related("created_by").order_by("-version")[:50]
            ],
            public_token=workspace.public_token,
            order=str(workspace.proposal.order_id) if workspace.proposal.order_id else None,
            publications=[
                {
                    "revision": p.revision,
                    "final": p.final,
                    "at": p.created_at.isoformat(),
                    "payload": p.payload,
                }
                for p in workspace.publications.order_by("-revision")
            ],
        )
    return result


def ensure_current(workspace, data):
    from common.locking import check_version

    check_version(workspace, data.get("version"))
    if data.get("revision") != workspace.revision:
        fail("Версия предложения изменилась; обновите страницу", "STALE_PROPOSAL", 409)


def ensure_active(workspace):
    if workspace.proposal.archived_at or workspace.proposal.status == "archived":
        fail("Предложение закрыто", "PROPOSAL_CLOSED", 409)
    publication = workspace.publications.filter(revision=workspace.revision).first()
    if not publication:
        fail("Предложение ещё не опубликовано", "NOT_FOUND", 404)
    until = parse_datetime(publication.payload.get("valid_until") or "")
    if until and until < timezone.now():
        fail("Срок действия предложения истёк", "PROPOSAL_EXPIRED", 409)
    return publication


def refresh_option(workspace, option_id):
    """Read-only supplier check outside DB locks, then merge using publication CAS.

    One option per request keeps provider latency bounded. Mock adapters are never
    used for customer-facing checks. Cached results protect supplier rate limits.
    """
    from integrations.adapters import (
        AdapterContext,
        AdapterError,
        ProviderAdapter,
        adapter_for_service,
        get_adapter,
    )
    from services.revalidation import _pricing

    publication = ensure_active(workspace)
    row = all_options(effective(workspace, publication)).get(option_id)
    if row is None:
        fail("Услуга не найдена", "NOT_FOUND", 404)
    if not auto_supported(row, workspace.proposal.tenant_id):
        return serialize(workspace, public=True)
    existing = workspace.checks.get(option_id, {})
    last = parse_datetime(existing.get("attempted_at") or "")
    if last and (timezone.now() - last).total_seconds() < 60:
        return serialize(workspace, public=True)
    revision = workspace.revision
    source, adapter = None, None
    if row.get("offer"):
        source = ServiceOffer.objects.filter(pk=row["offer"], tenant_id=workspace.proposal.tenant_id).first()
        if source and source.provider_adapter not in {"", "mock"}:
            adapter = get_adapter(source.provider_adapter)
            snapshot = source.raw_snapshot
    elif row.get("service"):
        source = OrderService.objects.filter(
            pk=row["service"], tenant_id=workspace.proposal.tenant_id, source="api"
        ).first()
        if source:
            adapter = adapter_for_service(source)
            snapshot = source.provider_snapshot
    if (
        not adapter
        or adapter.key in {"mock", "base"}
        or type(adapter).revalidate is ProviderAdapter.revalidate
    ):
        return serialize(workspace, public=True)
    now = timezone.now().isoformat()
    check = {"attempted_at": now, "auto_check": True}
    try:
        result = adapter.revalidate(
            AdapterContext(tenant_id=workspace.proposal.tenant_id, supplier_id=source.supplier_id),
            snapshot or {},
        )
        if result.get("status") in {"unavailable", "expired", "not_available"}:
            check.update(availability="unavailable", checked_at=now, check_source="api", check_error="")
        elif result.get("snapshot"):
            updated = result["snapshot"]
            price = _pricing(
                updated,
                source.supplier,
                workspace.proposal.tenant_id,
                **({"offer": source} if row.get("offer") else {"service": source}),
            )
            if updated["price"]["currency"] != publication.payload["currency"]:
                fail("Поставщик изменил валюту; требуется проверка оператором")
            client_base = Decimal(price["total"])
            if row.get("service"):
                client_base += sum(
                    (getattr(source, name) or Decimal(0) for name in ("taxes", "agency_fee", "markup")),
                    Decimal(0),
                ) - (source.discount or Decimal(0))
            new_price = amount(client_base + Decimal(row.get("price_offset", "0")))
            available = updated.get("availability", {})
            is_available = (
                available == "available"
                if isinstance(available, str)
                else available is True or (isinstance(available, dict) and available.get("available") is True)
            )
            check.update(
                price=new_price,
                previous_price=row.get("previous_price") or row["price"],
                availability="unknown"
                if available in ("on_request", "unknown")
                else "available"
                if is_available
                else "unavailable",
                checked_at=now,
                check_source="api",
                check_error="Условия изменились. Оператор должен проверить и опубликовать их заново."
                if result.get("terms_changed")
                else "",
            )
        else:
            check.update(check_error="Поставщик не подтвердил актуальность. Требуется проверка оператором.")
    except (AdapterError, ApiError):
        check.update(
            check_error="Проверка у поставщика не завершена. Повторите позже или обратитесь к оператору."
        )
    with transaction.atomic():
        current = (
            ProposalWorkspace.objects.select_for_update().select_related("proposal").get(pk=workspace.pk)
        )
        if current.revision != revision:
            fail("Опубликована новая версия; обновите страницу", "STALE_PROPOSAL", 409)
        # Another check already won. Do not overwrite newer results.
        if current.checks.get(option_id, {}).get("attempted_at", "") > now:
            return serialize(current, public=True)
        old = all_options(effective(current, publication))[option_id]
        changed = any(
            check.get(key, old.get(key)) != old.get(key) for key in ("price", "availability")
        ) or bool(check.get("check_error"))
        current.checks = {**current.checks, option_id: {**current.checks.get(option_id, {}), **check}}
        if changed and current.stage in {"approved", "awaiting_approval"}:
            current.stage = "review"
        current.version += 1
        current.save()
        record(
            current,
            "price_changed" if changed else "price_checked",
            {
                "option_id": option_id,
                "old_price": old["price"],
                "new_price": check.get("price", old["price"]),
            },
            notify=changed,
        )
        return serialize(current, public=True)


def workspace_summary(proposal):
    try:
        workspace = proposal.workspace
    except ProposalWorkspace.DoesNotExist:
        return None
    latest = workspace.publications.filter(revision=workspace.revision).first()
    payload = effective(workspace, latest) if latest else workspace.draft
    options = all_options(payload)
    return {
        "stage": workspace.stage,
        "label": STAGES[workspace.stage],
        "revision": workspace.revision,
        "title": workspace.draft.get("title", ""),
        "recipient": workspace.draft.get("recipient", proposal.recipient),
        "route": workspace.draft.get("route", ""),
        "travel_start": workspace.draft.get("travel_start", ""),
        "travel_end": workspace.draft.get("travel_end", ""),
        "currency": payload.get("currency", proposal.currency),
        "option_count": len(options),
        "group_count": len(payload.get("groups", [])),
        "has_draft": latest is None or workspace.draft != latest.payload,
        "saved_at": workspace.updated_at.isoformat(),
        "total": str(
            sum(
                (Decimal(options[oid]["price"]) for oid in workspace.selection.get("option_ids", [])),
                Decimal(0),
            )
        )
        if workspace.selection.get("option_ids") and set(workspace.selection["option_ids"]).issubset(options)
        else None,
        "service_kinds": list(dict.fromkeys(row["kind"] for row in options.values())),
    }
