"""Split stdCBS commands, confirmed synchronous by TTBooking support 2026-10-30.

Raw status responses are conservative: an unrecognised status remains pending;
HTTP 200 and an order token alone never imply ticket issuance.
"""

import hashlib
import hmac
import json
from collections import Counter
from copy import deepcopy
from datetime import date

from django.utils import timezone

from integrations.adapters import AdapterError, AmbiguousResultError
from integrations.supplier_lock import booking_lease


class TTBookingAsyncMixin:
    FILE_STATUSES = {
        "NEW": "booked",
        "COMPLETE": "issued",
        "CANCELLED": "cancelled",
        "VOID": "cancelled",
        "REFUND": "refunded",
        "ERROR": "failed",
    }
    CREATE_STATUSES = {
        "BOOKING": "booked",
        "SELL": "issued",
        "VOID": "cancelled",
        "REFUND": "refunded",
        "ISSUEING": "failed",
        "ERROR": "failed",
    }

    @staticmethod
    def _digest(value):
        from django.conf import settings

        return hmac.new(
            settings.SECRET_KEY.encode(),
            json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _passenger_key(self, passport):
        import re

        values = []
        for key in ("firstName", "lastName", "birthday", "number"):
            value = " ".join(str(passport.get(key) or "").strip().upper().split())
            if key == "birthday":
                value = value[:10]
            elif key == "number":
                value = re.sub(r"[\s-]", "", value)
            values.append(value)
        return self._digest(values)

    def _respond(self, result, *, mutation=False):
        data = result.get("respond") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise AdapterError(
                "BOOKING_UNKNOWN" if mutation else "INVALID_RESPONSE",
                "TTBooking: результат требует сверки" if mutation else "TTBooking: отсутствует respond",
                category="booking_unknown" if mutation else "internal",
            )
        messages = (data.get("messages") or {}).get("message") or []
        if isinstance(messages, dict):
            messages = [messages]
        has_error = any(
            isinstance(m, dict) and str(m.get("type", "")).upper() in ("ERROR", "FATAL") for m in messages
        )
        if result.get("error") or data.get("error") or data.get("errors") or has_error:
            raise AdapterError(
                "TTBOOKING_REJECTED",
                "TTBooking отклонил запрос; подробности в личном кабинете",
                category="booking_unknown" if mutation else "validation",
            )
        return data

    def _async_result(self, ctx, config, command, token):
        # RESULT is synchronous. Never poll or replay it implicitly.
        result = self._wire(ctx, config, command, {"token": token})
        self._respond(result)
        return result

    def _async_search(self, ctx, config, criteria):
        first = self._respond(self._wire(ctx, config, "SEARCHFLIGHTS", self._search_parameters(criteria)))
        token = first.get("token")
        if not token:
            return []
        response = self._async_result(ctx, config, "SEARCHRESULT", token)
        response["respond"]["token"] = token
        offers = self._normalize_search(response, config["currency"])
        for offer in offers:
            offer["provider_context"] = {
                "supplier_id": str(ctx.supplier_id),
                "environment": self.configured_environment(),
            }
        return offers

    def _async_revalidate(self, ctx, config, snapshot):
        self.assert_snapshot(ctx, snapshot)
        if snapshot.get("provider_adapter") != self.key or snapshot.get("provider_context", {}).get(
            "supplier_id"
        ) != str(ctx.supplier_id):
            raise AdapterError("AVAILABILITY_CONFLICT", "Оффер другого поставщика", category="validation")
        selection = deepcopy(snapshot["provider_selection"])
        first = self._respond(self._wire(ctx, config, "SELECTFLIGHT", selection))
        token = first.get("token")
        if not token:
            raise AdapterError(
                "SELECT_NOT_CONFIRMED", "TTBooking не подтвердил выбор тарифа", category="availability"
            )
        response = self._async_result(ctx, config, "SELECTRESULT", token)
        offers = self._normalize_search(response, config["currency"])
        expected = [
            (
                s.get("origin"),
                s.get("destination"),
                s.get("departure"),
                s.get("airline"),
                s.get("flight_number"),
            )
            for s in snapshot["itinerary"]["segments"]
        ]
        matches = [
            o
            for o in offers
            if [
                (
                    s.get("origin"),
                    s.get("destination"),
                    s.get("departure"),
                    s.get("airline"),
                    s.get("flight_number"),
                )
                for s in o["itinerary"]["segments"]
            ]
            == expected
        ]
        if len(matches) != 1:
            error = AdapterError(
                "RATE_UNAVAILABLE",
                "Не удалось однозначно подтвердить выбранные рейсы",
                category="availability",
            )
            error.selection_debug = {
                "matches": len(matches),
                "offers": len(offers),
                "expected": expected,
                "returned": [
                    [
                        (
                            s.get("origin"),
                            s.get("destination"),
                            s.get("departure"),
                            s.get("airline"),
                            s.get("flight_number"),
                        )
                        for s in o["itinerary"]["segments"]
                    ]
                    for o in offers
                ],
            }
            raise error
        fresh = matches[0]
        fresh["ttbooking"] = {
            "selection_token": token,
            "selected_group": deepcopy(fresh["provider_selection"]["flightsGroup"]),
        }
        fresh["provider_context"] = deepcopy(snapshot["provider_context"])
        fresh["provider_selection"] = deepcopy(snapshot["provider_selection"])
        fresh["provider_search_token"] = snapshot.get(
            "provider_search_token", snapshot["provider_selection"].get("token")
        )
        return {
            "status": "valid",
            "price": fresh["price"],
            "price_changed": fresh["price"] != snapshot["price"],
            "terms_changed": fresh["fare"] != snapshot.get("fare"),
            "snapshot": self.validated(ctx, fresh),
        }

    def _passport(self, person, departure, contact=None):
        document = person.get("document") or {}
        kinds = {
            "national_passport": "INTERNAL",
            "foreign_passport": "FOREIGN",
            "birth_certificate": "BIRTHDAY_NOTIFICATION",
            "id_card": "PASSPORT",
            "other": "PASSPORT",
        }
        first, last = person.get("latin_given_name"), person.get("latin_surname")
        if (
            not first
            or not last
            or document.get("type") not in kinds
            or not document.get("number")
            or not person.get("birth_date")
        ):
            raise AdapterError(
                "PASSENGER_DATA_MISSING",
                "TTBooking: нужны латинские ФИО, дата рождения и выбранный документ",
                category="validation",
            )
        try:
            born = date.fromisoformat(person["birth_date"])
        except (ValueError, TypeError):
            raise AdapterError(
                "PASSENGER_DATA_MISSING", "Проверьте дату рождения", category="validation"
            ) from None
        age = departure.year - born.year - ((departure.month, departure.day) < (born.month, born.day))
        if age < 0:
            raise AdapterError(
                "PASSENGER_DATA_MISSING", "Дата рождения позже даты вылета", category="validation"
            )
        gender = {"male": "MALE", "female": "FEMALE"}.get(person.get("gender"))
        if not gender or not person.get("citizenship"):
            raise AdapterError(
                "PASSENGER_DATA_MISSING", "Укажите пол и гражданство пассажира", category="validation"
            )
        passport = {
            "firstName": first,
            "lastName": last,
            "middleName": person.get("latin_middle_name", ""),
            "citizenship": {"code": person["citizenship"]},
            "type": kinds[document["type"]],
            "number": document["number"],
            "birthday": person["birth_date"] + "T00:00:00+00:00",
            "gender": gender,
        }
        for source, target in [("issued_at", "issued"), ("expires_at", "expired")]:
            if document.get(source):
                passport[target] = document[source] + "T00:00:00+00:00"
        import re

        contact = contact or {}
        email = str(person.get("email") or contact.get("email") or "").strip()
        phone = re.sub(r"\D", "", str(person.get("phone") or contact.get("phone") or ""))
        return {
            "passport": passport,
            "type": "INFANT" if age < 2 else "CHILD" if age < 12 else "ADULT",
            "phoneType": "HOME_PHONE",
            "phoneNumber": phone[-7:] if phone else "",
            "countryCode": phone[:-10] if phone else "",
            "areaCode": phone[-10:-7] if phone else "",
            "email": email,
            "isEmailRefused": False,
            "isEmailAbsent": not bool(email),
        }

    def _tt_result(self, service, snapshot, data, config, *, source="file"):
        booking = snapshot["ttbooking_booking"]
        file = data.get("bookingFile") or {}
        raw = file.get("status") if source == "file" else data.get("status")
        mapping = self.FILE_STATUSES if source == "file" else self.CREATE_STATUSES
        known = (
            set(self.FILE_STATUSES)
            | set(self.CREATE_STATUSES)
            | {
                "PROCESS",
                "BOOKING_NOT_CONFIRMED",
                "REQUEST_STATUS",
                "OPERATOR",
                "UNKNOWN",
                "EXCHANGE",
                "QUERY SENT",
                "QUERY READY",
            }
        )
        raw = raw.strip().upper() if isinstance(raw, str) and raw.strip().upper() in known else "UNRECOGNIZED"
        status = mapping.get(raw, "pending")
        token = data.get("token")
        if token and file:
            if booking.get("order_token") and str(token) != str(booking["order_token"]):
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "TTBooking: ответ относится к другому заказу",
                    category="booking_unknown",
                )
        reservations = self._list((file.get("reservations") or {}).get("reservation"))
        tickets = []
        complete = True
        deadlines = []
        from django.utils.dateparse import parse_datetime

        for reservation in reservations:
            value = reservation.get("timeLimit")
            if value:
                try:
                    deadline = parse_datetime(str(value))
                except ValueError:
                    deadline = None
                if deadline and timezone.is_aware(deadline):
                    deadlines.append(deadline)
            for ticket in self._list((reservation.get("products") or {}).get("airTicket")):
                if ticket.get("status") != "SELL":
                    complete = False
                    continue
                carrier = self._item(ticket.get("carrier"))["code"]
                number = str(ticket.get("number") or "").strip()
                pnr = str(ticket.get("recordLocator") or "").strip()
                identity = self._passenger_key((ticket.get("passenger") or {}).get("passport") or {})
                participant = (booking.get("passenger_keys") or {}).get(identity)
                if (
                    not carrier
                    or not number
                    or not pnr
                    or (booking.get("passenger_keys") and not participant)
                ):
                    complete = False
                    continue
                tickets.append(
                    {
                        "ticket_number": number,
                        "validating_carrier": carrier,
                        "pnr": pnr,
                        "participant_id": participant,
                        "status": "issued",
                    }
                )
        if deadlines:
            booking["ticketing_deadline"] = min(deadlines).isoformat()
        # COMPLETE/SELL alone is insufficient to store actual ticket records.
        expected = booking.get("passenger_count", 1)
        if status == "issued" and (
            not complete
            or len(tickets) < expected
            or (
                booking.get("passenger_keys")
                and {t["participant_id"] for t in tickets} != set(booking["passenger_keys"].values())
            )
        ):
            status = "pending"
            booking["needs_tickets"] = True
        else:
            booking.pop("needs_tickets", None)
        booking.update(
            normalized_status=status,
            supplier_status=raw,
            state="complete" if status != "pending" else "pending",
            updated_at=timezone.now().isoformat(),
        )
        if source == "create" and token and raw in ("BOOKING", "SELL", "VOID", "REFUND", "EXCHANGE"):
            booking["order_token"] = str(token)
        if status == "issued":
            booking.pop("issue_refusal", None)
        self._save(service, snapshot)
        result = {
            "locator": booking["locator"],
            "status": status,
            "supplier_status": raw,
            "provider_snapshot": deepcopy(snapshot),
        }
        if booking.get("ticketing_deadline"):
            result["ticketing_deadline"] = booking["ticketing_deadline"]
        if status == "issued":
            result["tickets"] = tickets
            pnrs = sorted({t["pnr"] for t in tickets})
            result["pnrs"] = pnrs
            if len(pnrs) == 1:
                result["pnr"] = pnrs[0]
        return result

    def _ticket_result(self, ctx, service, snapshot, booking, config, data):
        file = data.get("bookingFile") or {}
        if file.get("status") == "COMPLETE":
            data = self._respond(self._wire(ctx, config, "DISPLAYTICKETS", {"token": booking["order_token"]}))
        return self._tt_result(service, snapshot, data, config)

    def _async_book(self, ctx, request, *, resume=False):
        service = self._service(ctx, request.get("service_id"))
        with booking_lease(service):
            service.refresh_from_db()
            existing = (service.provider_snapshot or {}).get("ttbooking_booking")
            if existing and not resume:
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "TTBooking: используйте явное возобновление бронирования",
                    category="booking_unknown",
                )
            snapshot = (
                deepcopy(service.provider_snapshot)
                if existing
                else self.booking_snapshot(ctx, service.provider_snapshot)
            )
            self.assert_snapshot(ctx, snapshot)
            config = self._config(ctx)
            if existing and existing.get("order_token"):
                return self._async_retrieve(ctx, service, snapshot, snapshot["ttbooking_booking"], config)
            selected = snapshot.get("ttbooking") or {}
            if not selected.get("selection_token"):
                raise AdapterError(
                    "PREFLIGHT_REQUIRED", "Выполните перепроверку тарифа TTBooking", category="validation"
                )
            contact = request.get("contact_info") or {}
            if not all(str(contact.get(k) or "").strip() for k in ("name", "email", "phone")):
                raise AdapterError(
                    "CONTACT_REQUIRED",
                    "TTBooking: нужны имя, email и телефон контакта",
                    category="validation",
                )
            departure = date.fromisoformat(snapshot["itinerary"]["segments"][0]["departure"][:10])
            source = request.get("passengers") or []
            passengers = [self._passport(p, departure, contact) for p in source]
            expected = Counter(
                {
                    r["passengerType"]: int(r["count"])
                    for r in snapshot.get("fare", {}).get("passenger_prices", [])
                    if r.get("passengerType") and r.get("count")
                }
            )
            if not expected or Counter(p["type"] for p in passengers) != expected:
                raise AdapterError(
                    "PASSENGER_MISMATCH", "Состав пассажиров не соответствует тарифу", category="validation"
                )
            import re

            phone = re.sub(r"\D", "", contact["phone"])
            if len(phone) < 10:
                raise AdapterError(
                    "CONTACT_REQUIRED", "Проверьте международный телефон контакта", category="validation"
                )
            payload = {
                "token": selected["selection_token"],
                "flightsGroup": selected["selected_group"],
                "customer": {
                    "name": contact["name"],
                    "email": contact["email"],
                    "countryCode": phone[:-10],
                    "areaCode": phone[-10:-7],
                    "phoneNumber": phone[-7:],
                },
                "passengers": {"passenger": passengers},
            }
            digest = self._digest(payload)
            booking = snapshot["ttbooking_booking"] if existing else self.reserve(service, snapshot)
            if existing and booking.get("request_digest") != digest:
                raise AdapterError(
                    "RESUME_DATA_CHANGED",
                    "Данные контакта или пассажиров изменились после отправки. Нужна ручная сверка",
                    category="validation",
                )
            booking["request_digest"] = digest
            booking["passenger_count"] = len(passengers)
            booking["passenger_keys"] = {
                self._passenger_key(p["passport"]): str(original.get("participant_id") or "")
                for original, p in zip(source, passengers, strict=True)
            }
            self._save(service, snapshot)
            # Supplier explicitly guarantees idempotence for the SAME selection token.
            if not booking.get("create_token"):
                booking["create_sent"] = True
                self._save(service, snapshot)
                data = self._respond(
                    self._wire(ctx, config, "CREATEBOOKING", payload, mutation=True), mutation=True
                )
                if not data.get("token"):
                    raise AdapterError(
                        "BOOKING_UNKNOWN",
                        "TTBooking: идентификатор предброни не получен",
                        category="booking_unknown",
                    )
                booking["create_token"] = str(data["token"])
                self._save(service, snapshot)
            booking["create_result_sent"] = True
            self._save(service, snapshot)
            data = self._respond(
                self._wire(ctx, config, "CREATERESULT", {"token": booking["create_token"]}, mutation=True),
                mutation=True,
            )
            result = self._tt_result(service, snapshot, data, config, source="create")
            if booking.get("order_token"):
                return self._async_retrieve(ctx, service, snapshot, booking, config)
            return result

    def _async_retrieve(self, ctx, service, snapshot, booking, config):
        if not booking.get("order_token"):
            raise AdapterError(
                "BOOKING_RESUME_REQUIRED",
                "Возобновите бронирование явно: CREATERESULT может создавать бронь и не вызывается при проверке статуса",
                category="booking_unknown",
            )
        data = self._respond(self._wire(ctx, config, "DISPLAYSTATUS", {"token": booking["order_token"]}))
        return self._ticket_result(ctx, service, snapshot, booking, config, data)

    def _async_status(self, ctx, locator):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            return self._async_retrieve(ctx, service, snapshot, booking, self._config(ctx))

    def _async_mutation(self, ctx, locator, command, marker):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            config = self._config(ctx)
            current = self._async_retrieve(ctx, service, snapshot, booking, config)
            if (
                command == "PAYDEPOSIT"
                and current["status"] == "issued"
                or command == "CANCELBOOKING"
                and current["status"] == "cancelled"
            ):
                return current
            raw = current.get("supplier_status")
            allowed = {"NEW"} if command == "PAYDEPOSIT" else {"NEW", "COMPLETE"}
            if raw not in allowed or booking.get("needs_tickets"):
                raise AdapterError(
                    "BOOKING_NOT_CONFIRMED",
                    "Текущий статус TTBooking не разрешает эту операцию",
                    category="validation",
                )
            if command == "PAYDEPOSIT":
                from django.utils.dateparse import parse_datetime

                deadline = parse_datetime(booking.get("ticketing_deadline") or "")
                if not deadline or not timezone.is_aware(deadline) or deadline <= timezone.now():
                    raise AdapterError(
                        "TICKETING_DEADLINE",
                        "Тайм-лимит истёк или не подтверждён поставщиком",
                        category="validation",
                    )
            try:
                data = self.send_once(
                    ctx,
                    service,
                    snapshot,
                    marker,
                    lambda: self._wire(
                        ctx, config, command, {"token": booking["order_token"]}, mutation=True
                    ),
                )
                data = self._respond(data, mutation=True)
            except AdapterError as exc:
                if command == "PAYDEPOSIT" and exc.category == "booking_unknown":
                    raise AmbiguousResultError() from None
                raise
            messages = (data.get("messages") or {}).get("message") or []
            if isinstance(messages, dict):
                messages = [messages]
            insufficient = any(
                "NOT ENOUGHT MONEY" in str(m.get("message", "")).upper()
                for m in messages
                if isinstance(m, dict)
            )
            if (
                command == "PAYDEPOSIT"
                and (data.get("bookingFile") or {}).get("status") == "NEW"
                and insufficient
            ):
                # Synchronous, explicit insufficient-funds refusal: no debit or
                # issuance occurred. A later MANUAL request may retry after top-up.
                booking[marker] = False
                booking["issue_refusal"] = "INSUFFICIENT_DEPOSIT"
                booking["state"] = "complete"
                self._save(service, snapshot)
                raise AdapterError(
                    "INSUFFICIENT_DEPOSIT",
                    "На депозите TTBooking недостаточно средств. Пополните депозит вашего аккаунта и повторите выписку вручную",
                    category="financial",
                )
            result = self._tt_result(service, snapshot, data, config)
            if result.get("supplier_status") == "COMPLETE":
                return self._ticket_result(ctx, service, snapshot, booking, config, data)
            return result
