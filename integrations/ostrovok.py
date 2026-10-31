"""ETG API v3 (Ostrovok/RateHawk), from the supplier OpenAPI contract."""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from integrations.adapters import AdapterContext, AdapterError
from integrations.hotel_actions import api_hotel_capabilities
from integrations.json_provider import JsonProviderAdapter
from integrations.supplier_lock import booking_lease


class OstrovokAdapter(JsonProviderAdapter):
    key = "ostrovok"
    supported_kinds = ["hotel"]
    environment_setting = "OSTROVOK_ENVIRONMENT"
    default_url = "https://api.worldota.net"
    required_config = ("key_id", "api_key")
    requires_passenger_documents = False
    requires_booking_contact = True
    required_booking_contact_fields = ("email", "phone")

    def _call(self, ctx, config, operation, payload=None, *, mutation=False, method="POST"):
        auth = base64.b64encode(f"{config['key_id']}:{config['api_key']}".encode()).decode()
        response = self._http(
            ctx,
            config,
            operation.replace("/", "_"),
            method,
            "/api/b2b/v3/" + operation + "/",
            payload,
            headers={"Authorization": "Basic " + auth},
            mutation=mutation,
        )
        if not isinstance(response, dict):
            raise AdapterError(
                "INVALID_RESPONSE",
                "ETG: неверный формат ответа",
                category="booking_unknown" if mutation else "internal",
            )
        if response.get("status") != "ok":
            code = str(response.get("error") or "unknown")
            pending = code in ("timeout", "unknown", "processing", "not_ready", "booking_finish_in_progress")
            raise AdapterError(
                "ETG_PENDING" if pending else "ETG_REJECTED",
                "ETG: требуется сверка статуса" if pending else "ETG: запрос отклонён поставщиком",
                category="booking_unknown" if mutation or pending else "validation",
            )
        return response.get("data") or {}

    def verify_credentials(self, credential):
        ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=credential.supplier_id)
        _, config = self._config(ctx, credential)
        self._call(ctx, config, "general/contract/data/info", method="GET")
        return "ETG: доступ к API подтверждён"

    def locations(self, ctx, query):
        _, config = self._config(ctx)
        data = self._call(ctx, config, "search/multicomplete", {"query": query, "language": "ru"})
        return [
            {
                "city_code": str(r["id"]),
                "name": r["name"],
                "country": r.get("country_code", ""),
                "country_code": r.get("country_code", ""),
            }
            for r in data.get("regions", [])
            if r.get("type") in ("City", "Region")
        ]

    def hotel_booking_options(self, snapshot):
        return {"rooms": snapshot.get("ostrovok", {}).get("rooms", []), "allows_special_requests": True}

    def hotel_action_capabilities(self, service):
        return api_hotel_capabilities(service, "ostrovok_booking")

    def _hotel_content(self, ctx, config, hotel_id, *, fetch=False):
        # Supplier IDs and content never become universal CRM hotel identifiers.
        scope = json.dumps(
            [
                str(ctx.tenant_id),
                str(ctx.supplier_id),
                self.configured_environment(),
                config["url"],
                config["key_id"],
                str(hotel_id),
            ],
            separators=(",", ":"),
        )
        key = "etg:hotel:" + hashlib.sha256(scope.encode()).hexdigest()
        result = cache.get(key)
        if result is None and fetch:
            data = self._call(ctx, config, "hotel/info", {"id": str(hotel_id), "language": "ru"})
            result = {k: deepcopy(data[k]) for k in ("name", "address", "region", "star_rating") if k in data}
            cache.set(key, result, 86400)
        return result or {}

    def _offers(self, ctx, config, data, criteria):
        offers = []
        for hotel in data.get("hotels", []):
            content = self._hotel_content(ctx, config, hotel["id"])
            for rate in hotel.get("rates", []):
                payments = (rate.get("payment_options") or {}).get("payment_types") or []
                payment = next(
                    (
                        p
                        for p in payments
                        if p.get("type") == config.get("payment_type", "deposit")
                        and not p.get("is_need_credit_card_data")
                        and not p.get("is_need_cvc")
                    ),
                    None,
                )
                if not payment or not rate.get("book_hash"):
                    continue
                amount = Decimal(str(payment["amount"]))
                if not amount.is_finite() or amount < 0:
                    raise AdapterError("INVALID_PRICE", "ETG: некорректная цена", category="internal")
                rooms = [
                    {
                        "quantity": 1,
                        "adults": r["adults"],
                        "children": r.get("children", []),
                        "roomName": rate.get("room_name", ""),
                    }
                    for r in criteria["guests"]
                ]
                identity = {"supplier_id": str(ctx.supplier_id), "environment": self.configured_environment()}
                offers.append(
                    {
                        "provider_adapter": self.key,
                        "provider_context": identity,
                        "kind": "hotel",
                        "external_key": f"{hotel['id']}:{rate.get('match_hash') or rate['book_hash']}",
                        "price": {"amount": str(amount), "currency": payment["currency_code"]},
                        "availability": "available",
                        "expires_at": (timezone.now() + timedelta(minutes=10)).isoformat(),
                        "itinerary": {
                            "property_name": content.get("name")
                            or criteria.get("hotel_name")
                            or str(hotel["id"]),
                            "address": content.get("address", ""),
                            "city": (content.get("region") or {}).get("name", ""),
                            "check_in": criteria["checkin"],
                            "check_out": criteria["checkout"],
                            "room": rate.get("room_name", ""),
                            "meal_plan": rate.get("meal", ""),
                            "rooms_count": len(rooms),
                        },
                        "fare": {
                            "cancellation_rules": payment.get("cancellation_penalties") or {},
                            "meal": rate.get("meal", ""),
                        },
                        "ostrovok": {
                            "hotel_id": hotel["id"],
                            "hid": hotel.get("hid"),
                            "book_hash": rate["book_hash"],
                            "match_hash": rate.get("match_hash"),
                            "payment": deepcopy(payment),
                            "criteria": deepcopy(criteria),
                            "rooms": rooms,
                        },
                    }
                )
        return offers

    def search(self, ctx, kind, criteria):
        if kind != "hotel":
            raise AdapterError("NOT_SUPPORTED", "ETG: только гостиницы", category="validation")
        _, config = self._config(ctx)
        spec = criteria.get("ostrovok") or {}
        start, end = str(criteria.get("check_in") or ""), str(criteria.get("check_out") or "")
        try:
            if date.fromisoformat(start) >= date.fromisoformat(end):
                raise ValueError
        except ValueError:
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Укажите корректные даты проживания", category="validation"
            ) from None
        source = criteria.get("rooms") or [{"adults": criteria.get("adults", 1), "children": []}]
        guests = []
        for room in source:
            adults, children = room.get("adults", 1), room.get("childrenAges", room.get("children")) or []
            if (
                not isinstance(adults, int)
                or not 1 <= adults <= 6
                or any(not isinstance(a, int) or not 0 <= a < 18 for a in children)
            ):
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Укажите состав и возраст гостей", category="validation"
                )
            quantity = room.get("quantity", 1)
            if not isinstance(quantity, int) or not 1 <= quantity <= 5 or len(guests) + quantity > 5:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "Допустимо от 1 до 5 номеров", category="validation"
                )
            guests.extend([{"adults": adults, "children": children}] * quantity)
        payload = {
            "checkin": start,
            "checkout": end,
            "guests": guests,
            "residency": str(criteria.get("residency") or config.get("residency") or "").lower(),
            "currency": criteria.get("currency") or config.get("currency", "RUB"),
            "language": "ru",
            "timeout": 30,
        }
        if not payload["residency"]:
            raise AdapterError(
                "RESIDENCY_REQUIRED", "ETG: укажите страну резидентства гостей", category="validation"
            )
        if spec.get("hotel_id"):
            payload["id"] = str(spec["hotel_id"])
            operation = "search/hp"
        elif spec.get("region_id"):
            payload["region_id"] = int(spec["region_id"])
            operation = "search/serp/region"
        else:
            raise AdapterError(
                "LOCATION_REQUIRED", "Выберите регион или отель из справочника ETG", category="validation"
            )
        return self._offers(ctx, config, self._call(ctx, config, operation, payload), payload)

    def revalidate(self, ctx, offer_snapshot):
        self.assert_snapshot(ctx, offer_snapshot)
        _, config = self._config(ctx)
        old = offer_snapshot.get("ostrovok") or {}
        if offer_snapshot.get("provider_adapter") != self.key or (
            offer_snapshot.get("provider_context") or {}
        ).get("supplier_id") != str(ctx.supplier_id):
            raise AdapterError("AVAILABILITY_CONFLICT", "Оффер другого поставщика", category="validation")
        self._hotel_content(ctx, config, old["hotel_id"], fetch=True)
        # HP converts a SERP search hash into a bookable hotel-page rate.
        criteria = {**old["criteria"], "id": old["hotel_id"]}
        criteria.pop("region_id", None)
        fresh = self._offers(ctx, config, self._call(ctx, config, "search/hp", criteria), criteria)
        picked = next(
            (
                s
                for s in fresh
                if s["ostrovok"].get("match_hash") and s["ostrovok"]["match_hash"] == old.get("match_hash")
            ),
            None,
        )
        if picked is None:
            raise AdapterError(
                "RATE_UNAVAILABLE", "Выбранный тариф ETG больше не найден", category="availability"
            )
        checked = self._call(
            ctx,
            config,
            "hotel/prebook",
            {"hash": picked["ostrovok"]["book_hash"], "price_increase_percent": 0},
        )
        rates = self._offers(ctx, config, checked, criteria)
        picked = next((s for s in rates if s["ostrovok"].get("match_hash") == old.get("match_hash")), None)
        if picked is None:
            raise AdapterError(
                "RATE_UNAVAILABLE", "ETG не подтвердил выбранный тариф", category="availability"
            )
        for field in ("main_guest", "special_requests", "booking_user_ip"):
            if field in offer_snapshot:
                picked[field] = offer_snapshot[field]
        return {
            "status": "valid",
            "price": picked["price"],
            "price_changed": picked["price"] != offer_snapshot["price"],
            "terms_changed": picked["fare"] != offer_snapshot.get("fare"),
            "snapshot": self.validated(ctx, picked),
        }

    def _guests(self, request, snapshot):
        slots = snapshot["ostrovok"]["rooms"]
        guests = request.get("passengers") or []
        rooms = []
        checkin = date.fromisoformat(snapshot["itinerary"]["check_in"])
        for index, slot in enumerate(slots):
            assigned = [
                p
                for p in guests
                if p.get("room_ref") == f"room:{index}" or len(slots) == 1 and not p.get("room_ref")
            ]
            result = []
            ages = []
            for person in assigned:
                first, last = person.get("latin_given_name"), person.get("latin_surname")
                if not first or not last:
                    raise AdapterError(
                        "GUEST_DATA_REQUIRED", "ETG: нужны латинские имя и фамилия", category="validation"
                    )
                try:
                    born = date.fromisoformat(person["birth_date"])
                except (KeyError, ValueError, TypeError):
                    raise AdapterError(
                        "GUEST_DATA_REQUIRED",
                        "ETG: нужна корректная дата рождения гостя",
                        category="validation",
                    ) from None
                age = checkin.year - born.year - ((checkin.month, checkin.day) < (born.month, born.day))
                ages.append(age)
                result.append(
                    {
                        "first_name": first,
                        "last_name": last,
                        "gender": person.get("gender", ""),
                        "is_child": age < 18,
                        **({"age": age} if age < 18 else {}),
                    }
                )
            if sum(a >= 18 for a in ages) != slot["adults"] or sorted(a for a in ages if a < 18) != sorted(
                slot["children"]
            ):
                raise AdapterError(
                    "GUEST_ROOM_MISMATCH",
                    "ETG: гости не соответствуют составу номеров",
                    category="validation",
                )
            rooms.append({"guests": result})
        if sum(len(r["guests"]) for r in rooms) != len(guests):
            raise AdapterError(
                "GUEST_ROOM_MISMATCH", "ETG: есть нераспределённые гости", category="validation"
            )
        return rooms

    def book(self, ctx, request):
        service = self._service(ctx, request.get("service_id"))
        with booking_lease(service):
            service.refresh_from_db()
            if service.provider_snapshot.get("ostrovok_booking"):
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "ETG: создание уже отправлялось, проверьте статус",
                    category="booking_unknown",
                )
            snapshot = self.booking_snapshot(ctx, service.provider_snapshot)
            _, config = self._config(ctx)
            contact = request.get("contact_info") or {}
            if not contact.get("email") or not contact.get("phone"):
                raise AdapterError(
                    "CONTACT_REQUIRED", "ETG: у контактного лица нужны email и телефон", category="validation"
                )
            import ipaddress

            user_ip = snapshot.get("booking_user_ip") or config.get("user_ip")
            try:
                ipaddress.ip_address(user_ip)
            except (ValueError, TypeError):
                raise AdapterError(
                    "USER_IP_REQUIRED", "ETG: отсутствует IP пользователя оформления", category="validation"
                ) from None
            rooms = self._guests(request, snapshot)
            booking = self.reserve(service, snapshot)
            form = self.send_once(
                ctx,
                service,
                snapshot,
                "form_sent",
                lambda: self._call(
                    ctx,
                    config,
                    "hotel/order/booking/form",
                    {
                        "partner_order_id": booking["locator"],
                        "book_hash": snapshot["ostrovok"]["book_hash"],
                        "language": "ru",
                        "user_ip": user_ip,
                    },
                    mutation=True,
                ),
            )
            booking["order_id"] = form.get("order_id")
            payment = next(
                (
                    p
                    for p in form.get("payment_types", [])
                    if p.get("type") == config.get("payment_type", "deposit")
                    and not p.get("is_need_credit_card_data")
                    and not p.get("is_need_cvc")
                ),
                None,
            )
            expected = snapshot["price"]
            if (
                not payment
                or Decimal(str(payment.get("amount", "-1"))) != Decimal(expected["amount"])
                or payment.get("currency_code") != expected["currency"]
            ):
                self._save(service, snapshot)
                raise AdapterError(
                    "PRICE_CHANGED",
                    "ETG: изменились финансовые условия; нужна ручная сверка",
                    category="booking_unknown",
                )
            payload = {
                "partner": {
                    "partner_order_id": booking["locator"],
                    "comment": snapshot.get("special_requests", ""),
                },
                "payment_type": {k: payment[k] for k in ("type", "amount", "currency_code")},
                "rooms": rooms,
                "user": {"email": contact["email"], "phone": contact["phone"]},
                "language": "ru",
            }
            self.send_once(
                ctx,
                service,
                snapshot,
                "finish_sent",
                lambda: self._call(ctx, config, "hotel/order/booking/finish", payload, mutation=True),
            )
            booking["state"] = "pending"
            self._save(service, snapshot)
            return {"locator": booking["locator"], "status": "pending"}

    def _retrieve(self, ctx, service, snapshot, booking, config):
        if booking.get("finish_sent") and not booking.get("finish_checked"):
            checked = self._call(
                ctx, config, "hotel/order/booking/finish/status", {"partner_order_id": booking["locator"]}
            )
            if checked.get("partner_order_id") != booking["locator"]:
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "ETG: ответ о завершении относится к другому заказу",
                    category="booking_unknown",
                )
            if checked.get("percent") != 100:
                return {"locator": booking["locator"], "status": "pending"}
            booking["finish_checked"] = True
            self._save(service, snapshot)
        data = self._call(
            ctx,
            config,
            "hotel/order/info",
            {
                "search": {"partner_order_ids": [booking["locator"]]},
                "pagination": {"page_number": 1, "page_size": 10},
                "language": "ru",
            },
        )
        orders = data.get("orders") or []
        if len(orders) != 1 or (orders[0].get("partner_data") or {}).get("order_id") != booking["locator"]:
            raise AdapterError("BOOKING_UNKNOWN", "ETG: заказ требует сверки", category="booking_unknown")
        order = orders[0]
        raw = order.get("status")
        status = {"completed": "booked", "cancelled": "cancelled", "rejected": "failed"}.get(raw, "pending")
        booking.update(
            order_id=order.get("order_id"),
            normalized_status=status,
            supplier_status=raw,
            state="complete" if status != "pending" else "pending",
            is_cancellable=order.get("is_cancellable") is True,
            updated_at=timezone.now().isoformat(),
        )
        self._save(service, snapshot)
        return {"locator": booking["locator"], "status": status, "supplier_status": raw}

    def retrieve_booking(self, ctx, locator):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            _, config = self._config(ctx)
            return self._retrieve(ctx, service, snapshot, booking, config)

    def cancel(self, ctx, locator):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            _, config = self._config(ctx)
            result = self._retrieve(ctx, service, snapshot, booking, config)
            if result["status"] == "cancelled":
                return result
            if not booking.get("is_cancellable"):
                raise AdapterError("CANCEL_NOT_ALLOWED", "ETG не разрешает отмену", category="validation")
            booking["state"] = "cancelling"
            self.send_once(
                ctx,
                service,
                snapshot,
                "cancel_sent",
                lambda: self._call(
                    ctx, config, "hotel/order/cancel", {"partner_order_id": locator}, mutation=True
                ),
            )
            return self._retrieve(ctx, service, snapshot, booking, config)
