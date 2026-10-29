"""Read-only: ORDER_NUMBER=... python manage.py shell < scripts/diagnose_order_contact.py."""

import json
import os

from django.db import connection, transaction

from booking.job_handlers import _booking_contact
from orders.models import Order


def person_data(person):
    if person is None:
        return None
    fields = (
        "surname",
        "given_name",
        "middle_name",
        "first_name",
        "last_name",
        "latin_surname",
        "latin_given_name",
        "full_name",
    )
    return {
        "id": str(person.pk),
        **{key: getattr(person, key) for key in fields if hasattr(person, key)},
        "email_present": bool(str(person.email or "").strip()),
        "phone_present": bool(str(person.phone or "").strip()),
    }


def snapshot_contact(value):
    if not isinstance(value, dict):
        return None
    names = {
        "name",
        "full_name",
        "first_name",
        "last_name",
        "given_name",
        "surname",
        "contact_name",
        "contactName",
        "contact_person",
        "contactPerson",
        "contact",
        "contact_info",
    }
    result = {}
    for key, item in value.items():
        if key in names:
            result[key] = snapshot_contact(item) if isinstance(item, dict) else item
        elif isinstance(item, dict):
            nested = snapshot_contact(item)
            if nested:
                result[key] = nested
    return result


def order_data(order):
    participants = []
    for row in order.participants.select_related("person").order_by("created_at", "id"):
        participants.append(
            {
                "id": str(row.pk),
                "status": row.status,
                "role": row.role,
                "is_contact": row.is_contact,
                "person": person_data(row.person),
                "guest_snapshot_contact": snapshot_contact(row.guest_snapshot),
            }
        )
    contact = _booking_contact(order)
    old_person = order.contact_person or order.client_person
    company = order.client_company
    return {
        "id": str(order.pk),
        "number": order.number,
        "tenant_id": str(order.tenant_id),
        "status": order.status,
        "model_has_contact_name": hasattr(order, "contact_name"),
        "legacy_contact_name": getattr(order, "contact_name", None),
        "contact_person": person_data(order.contact_person),
        "client_person": person_data(order.client_person),
        "company": {"id": str(company.pk), "name": company.legal_name} if company else None,
        "snapshots": {
            field.name: snapshot_contact(getattr(order, field.name))
            for field in order._meta.concrete_fields
            if field.get_internal_type() == "JSONField"
        },
        "participants": participants,
        "previous_booking_contact": {
            "name": old_person.full_name if old_person else None,
            "source": "contact_person"
            if order.contact_person_id
            else "client_person"
            if old_person
            else "none",
        },
        "resolved_booking_contact": {
            "name": contact.get("name"),
            "email_present": bool(contact.get("email")),
            "phone_present": bool(contact.get("phone")),
        },
        "workflows": [
            {
                "id": str(flow.pk),
                "status": flow.status,
                "contact_error_count": flow.items.filter(error_code="CONTACT_REQUIRED").count(),
            }
            for flow in order.booking_workflows.order_by("-created_at")[:5]
        ],
    }


with transaction.atomic():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
    qs = Order.objects.select_related("contact_person", "client_person", "client_company").filter(
        number=os.environ.get("ORDER_NUMBER", "ORD-000017")
    )
    if os.environ.get("ORDER_TENANT_ID"):
        qs = qs.filter(tenant_id=os.environ["ORDER_TENANT_ID"])
    orders = list(qs[:2])
    if len(orders) != 1:
        print(
            json.dumps(
                {
                    "result": "not_found" if not orders else "tenant_required",
                    "matches": [{"id": str(row.pk), "tenant_id": str(row.tenant_id)} for row in orders],
                }
            )
        )
    else:
        order = orders[0]
        comparison = (
            Order.objects.select_related("contact_person", "client_person", "client_company")
            .filter(tenant_id=order.tenant_id)
            .exclude(pk=order.pk)
        )
        if os.environ.get("ORDER_COMPARE_NUMBER"):
            comparison = comparison.filter(number=os.environ["ORDER_COMPARE_NUMBER"])
        else:
            comparison = comparison.filter(booking_workflows__status="completed").distinct()
        other = comparison.order_by("-created_at").first()
        print(
            json.dumps(
                {"target": order_data(order), "comparison": order_data(other) if other else None},
                ensure_ascii=False,
                default=str,
                indent=2,
            )
        )
