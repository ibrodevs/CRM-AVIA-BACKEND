"""Canonical order contact: a selected CRM person or explicitly marked participant."""


def normalized_contact_text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _value(entity, key, default=None):
    return entity.get(key, default) if isinstance(entity, dict) else getattr(entity, key, default)


def contact_person_name(person):
    for fields in (
        ("surname", "given_name", "middle_name"),
        ("last_name", "first_name", "middle_name"),
        ("latin_surname", "latin_given_name", "latin_middle_name"),
    ):
        name = normalized_contact_text(
            " ".join(normalized_contact_text(_value(person, key)) for key in fields)
        )
        if name:
            return name
    return normalized_contact_text(_value(person, "full_name")) or normalized_contact_text(
        _value(person, "name")
    )


def _contact(person, source, participant=None):
    name = contact_person_name(person)
    return {
        "person": str(_value(person, "pk") or _value(person, "id"))
        if source != "participant_snapshot" and (_value(person, "pk") or _value(person, "id"))
        else None,
        "participant": str(participant.pk) if participant else None,
        "name": name,
        "email": normalized_contact_text(_value(person, "email")),
        "phone": normalized_contact_text(_value(person, "phone")),
        "source": source,
        "selected": True,
        "valid": bool(name),
        "message": "" if name else "У контактного лица не заполнено имя.",
    }


def resolve_order_contact(order):
    person = _value(order, "contact_person")
    if person is not None:
        return _contact(person, "contact_person")
    manager = _value(order, "participants")
    if manager is not None:
        cached = getattr(order, "_prefetched_objects_cache", {}).get("participants")
        rows = (
            cached
            if cached is not None
            else manager.select_related("person")
            .filter(is_contact=True, status="active")
            .order_by("created_at", "id")
        )
        contacts = [row for row in rows if row.is_contact and row.status == "active"]
        if len(contacts) == 1:
            row = contacts[0]
            return _contact(
                row.person if row.person_id else row.guest_snapshot or {},
                "participant" if row.person_id else "participant_snapshot",
                row,
            )
        if len(contacts) > 1:
            return {
                "person": None,
                "participant": None,
                "name": "",
                "email": "",
                "phone": "",
                "source": "ambiguous",
                "selected": False,
                "valid": False,
                "message": "Укажите контактное лицо заказа.",
            }
    person = _value(order, "client_person")
    if person is not None:
        return _contact(person, "client_person")
    # Legacy in-memory/import contracts can carry a name; modern Order has no such column.
    legacy = _value(order, "contact_name")
    if legacy is not None:
        return _contact({"name": legacy}, "legacy_name")
    return {
        "person": None,
        "participant": None,
        "name": "",
        "email": "",
        "phone": "",
        "source": "none",
        "selected": False,
        "valid": False,
        "message": "Укажите контактное лицо заказа.",
    }


def booking_contact_info(order):
    contact = resolve_order_contact(order)
    return {key: contact[key] for key in ("name", "email", "phone")} if contact["selected"] else {}
