from decimal import Decimal

from common.errors import ApiError


def require_proposal_approval(service):
    """A prepared KP service cannot bypass approval by starting a separate workflow."""
    binding = (service.provider_snapshot or {}).get("proposal_approval")
    if not binding:
        return
    from offers.models import ProposalWorkspace
    from offers.workspace import all_options, effective, ensure_active

    ws = ProposalWorkspace.objects.filter(
        proposal_id=binding.get("proposal"), proposal__tenant_id=service.tenant_id
    ).first()
    publication = ws.publications.filter(revision=binding.get("revision")).first() if ws else None
    valid = (
        ws
        and publication
        and ws.stage == "approved"
        and ws.revision == binding.get("revision")
        and ws.selection.get("revision") == ws.revision
        and ws.draft == publication.payload
    )
    if valid:
        ensure_active(ws)
        payload = effective(ws, publication)
        rows = all_options(payload)
        ids = ws.selection.get("option_ids", [])
        valid = binding.get("option") in ids and all(
            rows.get(i, {}).get("availability") == "available" and not rows.get(i, {}).get("check_error")
            for i in ids
        )
        if valid:
            valid = (
                sum((Decimal(rows[i]["price"]) for i in ids), Decimal(0))
                == Decimal(str(ws.selection["total"]))
                and Decimal(rows[binding["option"]]["price"]) == service.client_total
                and payload["currency"] == service.currency
            )
    if not valid:
        raise ApiError(
            code="PROPOSAL_APPROVAL_REQUIRED",
            message="Условия КП изменились. Требуется согласование клиентом актуальной версии перед бронированием.",
            status_code=409,
        )
