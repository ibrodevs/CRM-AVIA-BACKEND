"""i'way v4, using the supplied integration manual (not the older v3 mirror)."""

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core import signing
from django.core.cache import cache
from django.utils import timezone

from integrations.adapters import AdapterContext, AdapterError, AmbiguousResultError
from integrations.json_provider import JsonProviderAdapter
from integrations.supplier_lock import booking_lease


class IwayAdapter(JsonProviderAdapter):
    key = "iway"
    supported_kinds = ["transfer"]
    environment_setting = "IWAY_ENVIRONMENT"
    required_config = ("login", "password", "user_id")
    requires_passenger_documents = False
    requires_booking_contact = True
    required_booking_contact_fields = ("email", "phone")

    def _config(self, ctx, credential=None):
        credential, config = super()._config(ctx, credential)
        if not config["url"]:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "Укажите URL iway из конфигурации поставщика",
                category="configuration",
            )
        try:
            config["user_id"] = int(config["user_id"])
            if config["user_id"] <= 0:
                raise ValueError
        except (ValueError, TypeError):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "iway: user_id должен быть положительным числом",
                category="configuration",
            ) from None
        return credential, config

    def _token_key(self, ctx, credential, config):
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        key = f"iway:auth:{ctx.tenant_id}:{ctx.supplier_id}:{credential.pk}:{self.configured_environment()}:{digest}"
        return key

    def _token(self, ctx, credential, config, *, fresh=False):
        key = self._token_key(ctx, credential, config)
        token = None if fresh else cache.get(key)
        if not token:
            result = self._http(
                ctx,
                config,
                "login",
                "POST",
                "/v4/auth/login",
                {"login": config["login"], "password": config["password"]},
            )
            result = result.get("result") if isinstance(result, dict) else None
            if isinstance(result, list) and len(result) == 1:
                result = result[0]
            token = result.get("token") if isinstance(result, dict) else None
            if not token:
                raise AdapterError("AUTH_ERROR", "iway не подтвердил авторизацию", category="auth")
            cache.set(key, token, 6 * 86400)
        return token

    def _call(
        self, ctx, credential, config, operation, method="GET", payload=None, *, params=None, mutation=False
    ):
        query = urlencode({"lang": "ru", "user_id": config["user_id"], **(params or {})}, doseq=True)
        try:
            response = self._http(
                ctx,
                config,
                operation.replace("/", "_"),
                method,
                "/v4/" + operation + "?" + query,
                payload,
                headers={"Authorization": "Bearer " + self._token(ctx, credential, config)},
                mutation=mutation,
            )
        except AdapterError as exc:
            if exc.category == "auth":
                cache.delete(self._token_key(ctx, credential, config))
            # Never replay a mutation after reauthentication.
            raise
        if not isinstance(response, dict) or response.get("error"):
            raise AdapterError(
                "IWAY_REJECTED",
                "iway отклонил запрос",
                category="booking_unknown" if mutation else "validation",
            )
        return response.get("result")

    def verify_credentials(self, credential):
        ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=credential.supplier_id)
        credential, config = self._config(ctx, credential)
        self._token(ctx, credential, config, fresh=True)
        return "iway: авторизация подтверждена"

    @staticmethod
    def _point(value):
        if isinstance(value, dict):
            value = f"{value.get('lat')},{value.get('lon')}"
        try:
            lat, lon = map(float, str(value).split(","))
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError
        except (ValueError, TypeError):
            raise AdapterError(
                "COORDINATES_REQUIRED",
                "Для iway выберите точки с координатами (широта,долгота)",
                category="validation",
            ) from None
        return f"{lat},{lon}"

    def _normalize(self, ctx, source, criteria):
        amount = Decimal(str(source["price"]))
        if not amount.is_finite() or amount < 0:
            raise AdapterError("INVALID_PRICE", "iway: некорректная цена", category="internal")
        options = source.get("additional_services") or []
        requested = criteria.get("additional_service_types") or []
        allowed = {"table", "infant_seat", "seat", "booster", "tollroad"}
        if (
            not isinstance(requested, list)
            or len(requested) != len(set(requested))
            or any(t not in allowed for t in requested)
        ):
            raise AdapterError(
                "EXTRA_INVALID", "Выберите не более одной доп. услуги каждого типа", category="validation"
            )
        selected = []
        for kind in requested:
            candidates = [r for r in options if r.get("type") == kind]
            if len(candidates) != 1:
                raise AdapterError(
                    "EXTRA_UNAVAILABLE",
                    "Дополнительная услуга недоступна в этом предложении: " + kind,
                    category="availability",
                )
            extra = deepcopy(candidates[0])
            if (
                kind == "table"
                and source.get("flexible_tariff_agreement") is False
                and not extra.get("default_include")
            ):
                raise AdapterError(
                    "EXTRA_UNAVAILABLE",
                    "Опциональная встреча с табличкой недоступна по договору",
                    category="availability",
                )
            if extra.get("id") is None or (not extra.get("default_include") and extra.get("price") is None):
                raise AdapterError(
                    "EXTRA_UNAVAILABLE",
                    "Поставщик не подтвердил ID или цену доп. услуги",
                    category="availability",
                )
            if not extra.get("default_include"):
                cost = Decimal(str(extra["price"]))
                if (
                    not cost.is_finite()
                    or cost < 0
                    or extra.get("currency", source["currency"]) != source["currency"]
                ):
                    raise AdapterError(
                        "INVALID_PRICE", "iway: некорректная цена доп. услуги", category="internal"
                    )
                amount += cost
            selected.append(extra)
        car = source.get("car_class") or {}
        return {
            "provider_adapter": self.key,
            "provider_context": {
                "supplier_id": str(ctx.supplier_id),
                "environment": self.configured_environment(),
            },
            "external_key": str(source.get("price_uid") or source["price_id"]),
            "kind": "transfer",
            "price": {"amount": str(amount), "currency": source["currency"]},
            "availability": "available",
            "expires_at": (timezone.now() + timedelta(minutes=10)).isoformat(),
            "itinerary": {
                "description": car.get("title") or "Трансфер iway",
                "origin": criteria.get("origin_address", ""),
                "destination": criteria.get("destination_address", ""),
                "pickup_time": criteria.get("pickup_time"),
                "car_class": car.get("title"),
                "capacity": car.get("capacity"),
                "luggage_capacity": car.get("luggage_capacity"),
            },
            "fare": {
                "cancellation_rules": {
                    "free_cancellation_hours": source.get("cancellation_time"),
                    "penalty_after_deadline_percent": 100,
                },
                "included_services": source.get("class_services") or [],
                "additional_services": source.get("additional_services") or [],
                "selected_services": selected,
            },
            "iway": {
                "criteria": deepcopy(criteria),
                "price_id": source["price_id"],
                "price_uid": source.get("price_uid"),
                "start_place": source.get("start_place") or {},
                "finish_place": source.get("finish_place") or {},
                "car_class": car,
                "selected_services": selected,
                "included_services": [r for r in options if r.get("default_include")],
                "flexible_tariff": source.get("flexible_tariff"),
            },
        }

    def search(self, ctx, kind, criteria):
        if kind != "transfer":
            raise AdapterError("NOT_SUPPORTED", "iway: только трансферы", category="validation")
        credential, config = self._config(ctx)
        if criteria.get("return_date"):
            raise AdapterError(
                "SEPARATE_RETURN_REQUIRED",
                "Обратный трансфер оформляется отдельной поездкой",
                category="validation",
            )
        spec = criteria.get("iway") or {}
        values = {**criteria, **spec}
        values["start_place_point"] = self._point(values.get("start_place_point"))
        values["finish_place_point"] = self._point(values.get("finish_place_point"))
        params = {
            k: values[k] for k in ("start_place_point", "finish_place_point", "currency") if values.get(k)
        }
        rows = self._call(ctx, credential, config, "prices", params={**params, "platform": 7})
        if not isinstance(rows, list):
            raise AdapterError(
                "INVALID_RESPONSE", "iway: отсутствует список предложений", category="internal"
            )
        offers = []
        for row in rows:
            if row.get("service_id") != 1:
                continue
            try:
                offers.append(self._normalize(ctx, row, values))
            except AdapterError as exc:
                if exc.code != "EXTRA_UNAVAILABLE":
                    raise
        return offers

    def revalidate(self, ctx, offer_snapshot):
        self.assert_snapshot(ctx, offer_snapshot)
        credential, config = self._config(ctx)
        spec = offer_snapshot.get("iway") or {}
        if offer_snapshot.get("provider_context", {}).get("supplier_id") != str(ctx.supplier_id):
            raise AdapterError("AVAILABILITY_CONFLICT", "Оффер другого поставщика", category="validation")
        criteria = spec["criteria"]
        rows = self._call(
            ctx,
            credential,
            config,
            "prices",
            params={
                "start_place_point": criteria["start_place_point"],
                "finish_place_point": criteria["finish_place_point"],
                "currency": offer_snapshot["price"]["currency"],
                "current_price_id": spec["price_id"],
                "platform": 7,
            },
        )
        row = next((r for r in rows or [] if str(r.get("price_id")) == str(spec["price_id"])), None)
        if not row:
            raise AdapterError(
                "RATE_UNAVAILABLE", "Предложение iway больше не доступно", category="availability"
            )
        fresh = self._normalize(ctx, row, criteria)
        return {
            "status": "valid",
            "price": fresh["price"],
            "price_changed": fresh["price"] != offer_snapshot["price"],
            "terms_changed": fresh["fare"] != offer_snapshot.get("fare"),
            "snapshot": self.validated(ctx, fresh),
        }

    @staticmethod
    def _one(value):
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if not isinstance(value, dict) or not value.get("order_id") or not value.get("transaction"):
            raise AdapterError(
                "BOOKING_UNKNOWN", "iway: нет однозначного результата заказа", category="booking_unknown"
            )
        return value

    def _result(self, service, snapshot, data):
        booking = snapshot["iway_booking"]
        raw, paid = str(data.get("status")), str(data.get("payable_status"))
        raw = raw if raw in {"0", "1", "2", "3", "4", "5", "6", "7"} else "unknown"
        paid = paid if paid in {"0", "1", "5"} else "unknown"
        status = (
            "cancelled"
            if raw in ("4", "5")
            else "issued"
            if raw in ("2", "3") and paid == "1"
            else "booked"
            if raw in ("0", "1", "2", "3") and paid == "0" or raw in ("2", "3")
            else "pending"
        )
        booking.update(
            order_id=data["order_id"],
            transaction=data["transaction"],
            supplier_status=raw,
            payable_status=paid,
            payment_confirmed=paid == "1",
            normalized_status=status,
            state="complete" if status != "pending" else "pending",
            updated_at=timezone.now().isoformat(),
        )
        # A penalty quote must use the fresh supplier order cost, not the CRM markup.
        price = data.get("price")
        booking.pop("current_supplier_price", None)
        if isinstance(price, (int, float, str)) and data.get("currency"):
            try:
                total = Decimal(str(price))
                if total.is_finite() and total >= 0:
                    booking["current_supplier_price"] = {
                        "amount": format(total, "f"),
                        "currency": data["currency"],
                    }
            except ArithmeticError:
                pass
        if (
            isinstance(price, dict)
            and price.get("price") is not None
            and price.get("price_subaddress") in (None, 0, "0", "0.00")
            and data.get("currency")
        ):
            try:
                # V4 order price already includes selected extras. Their list is
                # informational; adding it again double-charges e.g. toll roads.
                total = Decimal(str(price["price"]))
                if total.is_finite() and total >= 0:
                    booking["current_supplier_price"] = {
                        "amount": format(total, "f"),
                        "currency": data["currency"],
                    }
            except (ValueError, KeyError, ArithmeticError):
                pass
        self._save(service, snapshot)
        return {
            "locator": booking["locator"],
            "status": status,
            "supplier_status": raw,
            "payment_confirmed": paid == "1",
        }

    def can_resume_booking(self, service):
        booking = (service.provider_snapshot or {}).get("iway_booking") or {}
        return bool(
            booking.get("create_sent")
            and not booking.get("transaction")
            and not booking.get("approve_sent")
            and not booking.get("cancel_sent")
            and not booking.get("order_id")
            and len(booking.get("abandoned_attempts", [])) < 3
        )

    def resume_booking(self, ctx, request):
        return self.book(ctx, request, resume=True)

    def book(self, ctx, request, *, resume=False):
        service = self._service(ctx, request.get("service_id"))
        with booking_lease(service):
            service.refresh_from_db()
            existing = service.provider_snapshot.get("iway_booking")
            if existing and (not resume or not self.can_resume_booking(service)):
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "iway: заказ уже отправлялся; проверьте статус",
                    category="booking_unknown",
                )
            original = deepcopy(service.provider_snapshot)
            if existing:
                original.pop("iway_booking", None)
                original.pop("validation", None)
            snapshot = self.booking_snapshot(ctx, original)
            credential, config = self._config(ctx)
            spec, contact = snapshot["iway"], request.get("contact_info") or {}
            criteria = spec["criteria"]
            if (
                not criteria.get("pickup_time")
                or not criteria.get("origin_address")
                or not criteria.get("destination_address")
                or not contact.get("phone")
                or not contact.get("email")
            ):
                raise AdapterError(
                    "TRANSFER_DATA_REQUIRED",
                    "Укажите время подачи, адреса и телефон контакта",
                    category="validation",
                )
            start_type = str(spec["start_place"].get("type"))
            flight, train = criteria.get("flight_number"), criteria.get("train_number")
            if start_type == "2" and not flight or start_type == "3" and not train:
                raise AdapterError(
                    "ARRIVAL_DATA_REQUIRED",
                    "Укажите номер прибывающего рейса или поезда",
                    category="validation",
                )
            passengers = [
                {
                    "phone": str(p.get("phone") or contact.get("phone") or "").strip(),
                    "email": str(p.get("email") or contact.get("email") or "").strip(),
                    "name": " ".join(
                        filter(
                            None,
                            [
                                p.get("surname") or p.get("latin_surname"),
                                p.get("given_name") or p.get("latin_given_name"),
                            ],
                        )
                    ),
                }
                for p in request.get("passengers") or []
            ]
            if (
                not passengers
                or any(not p["name"] for p in passengers)
                or len(passengers) > int(spec["car_class"].get("capacity") or 0)
            ):
                raise AdapterError(
                    "PASSENGERS_REQUIRED", "Проверьте гостей и вместимость автомобиля", category="validation"
                )
            try:
                pickup = date.fromisoformat(criteria["pickup_time"][:10])
                births = [date.fromisoformat(p["birth_date"]) for p in request.get("passengers") or []]
            except (KeyError, ValueError, TypeError):
                raise AdapterError(
                    "PASSENGER_DATA_MISSING",
                    "Укажите дату рождения всех пассажиров и корректную дату подачи",
                    category="validation",
                ) from None
            ages = [pickup.year - b.year - ((pickup.month, pickup.day) < (b.month, b.day)) for b in births]
            if any(age < 0 for age in ages):
                raise AdapterError(
                    "PASSENGER_DATA_MISSING", "Дата рождения позже даты поездки", category="validation"
                )
            required_seats = [
                "infant_seat" if a < 1 else "seat" if a < 3 else "booster" for a in ages if a < 12
            ]
            supplied_seats = {
                r.get("type") for r in spec.get("selected_services", []) + spec.get("included_services", [])
            }
            if len(required_seats) != len(set(required_seats)) or any(
                kind not in supplied_seats for kind in required_seats
            ):
                raise AdapterError(
                    "CHILD_SEAT_REQUIRED",
                    "Для каждого ребёнка младше 12 лет нужно подходящее кресло. При нескольких креслах одного типа требуется согласование с оператором",
                    category="validation",
                )
            try:
                datetime.strptime(criteria["pickup_time"], "%Y-%m-%d %H:%M")
                if criteria.get("departure_time"):
                    datetime.strptime(criteria["departure_time"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                raise AdapterError(
                    "TRANSFER_TIME_INVALID",
                    "Время поездки: YYYY-MM-DD HH:MM в часовом поясе места подачи",
                    category="validation",
                ) from None
            if (
                start_type not in ("2", "3")
                and str(spec["finish_place"].get("type")) in ("2", "3")
                and not criteria.get("departure_time")
            ):
                raise AdapterError(
                    "DEPARTURE_TIME_REQUIRED",
                    "Укажите местное время отправления рейса или поезда",
                    category="validation",
                )
            time_check = self._call(
                ctx,
                credential,
                config,
                "orders/check-time",
                "POST",
                {
                    "price_id": spec["price_id"],
                    "pickup_time": criteria["pickup_time"],
                    **(
                        {"departure_time": criteria["departure_time"]}
                        if criteria.get("departure_time")
                        else {}
                    ),
                },
            )
            if isinstance(time_check, list) and len(time_check) == 1:
                time_check = time_check[0]
            if (
                not isinstance(time_check, dict)
                or time_check.get("allow_booking") is not True
                or time_check.get("possible_pickup_time") is False
                or time_check.get("possible_connecting_time") is False
            ):
                raise AdapterError(
                    "TRANSFER_TIME_UNAVAILABLE",
                    "iway не подтвердил возможность поездки в указанное время",
                    category="availability",
                )
            if time_check.get("similar_orders") and not existing:
                raise AdapterError(
                    "POSSIBLE_DUPLICATE",
                    "iway обнаружил похожую поездку. Проверьте существующий заказ у поставщика",
                    category="validation",
                )
            if existing:
                snapshot["iway_booking"] = {
                    "locator": existing["locator"],
                    "state": "creating",
                    "normalized_status": "pending",
                    "abandoned_attempts": [
                        *existing.get("abandoned_attempts", []),
                        {
                            "create_sent": existing.get("create_sent"),
                            "state": existing.get("state"),
                            "abandoned_at": timezone.now().isoformat(),
                            "reason": "Lost unapproved create response; supplier permits replacement",
                        },
                    ],
                }
                booking = snapshot["iway_booking"]
                self._save(service, snapshot)
            else:
                booking = self.reserve(service, snapshot)
            start = {
                "time": criteria["pickup_time"],
                "address": criteria["origin_address"],
                "location": criteria["start_place_point"],
            }
            if flight:
                start["flight_number"] = flight
            if train:
                start["train_number"] = train
            trip = {
                "price_id": spec["price_id"],
                "currency": snapshot["price"]["currency"],
                "start_location": start,
                "finish_location": {
                    "address": criteria["destination_address"],
                    "location": criteria["finish_place_point"],
                    **({"time": criteria["departure_time"]} if criteria.get("departure_time") else {}),
                },
                "passengers_number": len(passengers),
                "adults_amount": sum(a >= 12 for a in ages),
                "children_amount": sum(a < 12 for a in ages),
                "passengers": passengers,
                "platform": 7,
                "additional_services": [
                    {"id": r["id"]} for r in spec.get("selected_services", []) if not r.get("default_include")
                ],
                "customer": {k: contact.get(k, "") for k in ("name", "phone", "email")},
                "internal_number": booking["locator"],
                "send_params": {
                    key: False
                    for key in (
                        "send_client_voucher",
                        "send_admin_voucher",
                        "send_client_doc",
                        "send_admin_doc",
                    )
                },
            }
            if start_type in ("2", "3"):
                trip["text_tablet"] = criteria.get("text_tablet") or passengers[0]["name"]
            booking["agreed_price"] = deepcopy(snapshot["price"])
            self._save(service, snapshot)
            data = self.send_once(
                ctx,
                service,
                snapshot,
                "create_sent",
                lambda: self._call(
                    ctx, credential, config, "orders", "POST", {"lang": "ru", "trips": [trip]}, mutation=True
                ),
            )
            return self._result(service, snapshot, self._one(data))

    def _retrieve(self, ctx, service, snapshot, booking, credential, config):
        if booking.get("abandoned_unapproved"):
            return {
                "locator": booking["locator"],
                "status": "cancelled",
                "local_only": True,
                "notice": booking.get("notice", ""),
            }
        if not booking.get("transaction"):
            # The contract has no lookup by internal_number. Never create again.
            raise AdapterError(
                "BOOKING_UNKNOWN",
                "iway: ответ создания потерян. Можно явно возобновить создание нового неподтверждённого заказа; internal_number не поддерживает поиск.",
                category="booking_unknown",
            )
        result = self._call(
            ctx,
            credential,
            config,
            "orders/trips",
            params={"transaction": booking["transaction"], "order_id": booking["order_id"]},
        )
        rows = result.get("orders") if isinstance(result, dict) else None
        matches = [
            r
            for r in rows or []
            if str(r.get("order_id")) == str(booking["order_id"])
            and str(r.get("transaction")) == str(booking["transaction"])
        ]
        if len(matches) != 1:
            raise AdapterError(
                "BOOKING_UNKNOWN", "iway: поездка не найдена однозначно", category="booking_unknown"
            )
        return self._result(service, snapshot, matches[0])

    def retrieve_booking(self, ctx, locator):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            credential, config = self._config(ctx)
            return self._retrieve(ctx, service, snapshot, booking, credential, config)

    def issue(self, ctx, request):
        service, _, _ = self._stored(ctx, request["locator"])
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, request["locator"])
            credential, config = self._config(ctx)
            current = self._retrieve(ctx, service, snapshot, booking, credential, config)
            if current["status"] == "issued":
                return current
            if current["status"] != "booked":
                raise AdapterError(
                    "BOOKING_NOT_CONFIRMED", "Поездка ещё не принята поставщиком", category="validation"
                )
            actual = booking.get("current_supplier_price")
            agreed = booking.get("agreed_price") or snapshot["price"]
            if actual and (
                actual["currency"] != agreed["currency"]
                or Decimal(actual["amount"]) != Decimal(agreed["amount"])
            ):
                raise AdapterError(
                    "PRICE_CHANGED",
                    "Стоимость заказа iway изменилась. Согласуйте новую сумму до подтверждения оплаты",
                    category="price",
                )
            try:
                data = self.send_once(
                    ctx,
                    service,
                    snapshot,
                    "approve_sent",
                    lambda: self._call(
                        ctx,
                        credential,
                        config,
                        "orders/approve/" + str(booking["transaction"]),
                        "POST",
                        {},
                        mutation=True,
                    ),
                )
            except AdapterError as exc:
                if exc.category == "booking_unknown":
                    raise AmbiguousResultError() from None
                raise
            return self._result(service, snapshot, self._one(data))

    def _cancel_quote(self, ctx, service, snapshot, booking, credential, config):
        current = self._retrieve(ctx, service, snapshot, booking, credential, config)
        if current["status"] == "cancelled":
            return {"cancelled": True, "locator": booking["locator"]}
        if booking.get("payable_status") == "0" and not booking.get("approve_sent"):
            return {
                "tenant": str(ctx.tenant_id),
                "supplier": str(ctx.supplier_id),
                "service": str(service.pk),
                "locator": booking["locator"],
                "local_only": True,
                "supplier_cancel_status": "unapproved_draft",
                "penalty": "0",
                "currency": snapshot["price"]["currency"],
                "percent": 0,
            }
        check = self._call(ctx, credential, config, "orders/" + str(booking["order_id"]) + "/check-cancel")
        if (
            not isinstance(check, dict)
            or check.get("result") is not True
            or str(check.get("status")) not in ("4", "5")
        ):
            raise AdapterError(
                "CANCEL_NOT_ALLOWED", "iway не разрешает отмену поездки", category="validation"
            )
        paid = str(check["status"]) == "5"
        price = booking.get("current_supplier_price") if paid else snapshot["price"]
        if not price:
            raise AdapterError(
                "CANCEL_PRICE_UNKNOWN",
                "Штраф 100%, но текущая стоимость заказа не подтверждена API. Требуется сверка с оператором",
                category="validation",
            )
        quote = {
            "tenant": str(ctx.tenant_id),
            "supplier": str(ctx.supplier_id),
            "service": str(service.pk),
            "locator": booking["locator"],
            "supplier_cancel_status": str(check["status"]),
            "penalty": price["amount"] if paid else "0",
            "currency": price["currency"],
            "percent": 100 if paid else 0,
        }
        return quote

    def cancellation_quote(self, ctx, locator):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            credential, config = self._config(ctx)
            quote = self._cancel_quote(ctx, service, snapshot, booking, credential, config)
            return {
                **quote,
                "confirmation": signing.dumps(quote, salt="iway-cancel"),
                "valid_for_seconds": 60,
            }

    def cancel(self, ctx, locator, *, confirmation=None):
        service, _, _ = self._stored(ctx, locator)
        with booking_lease(service):
            service, snapshot, booking = self._stored(ctx, locator)
            credential, config = self._config(ctx)
            quote = self._cancel_quote(ctx, service, snapshot, booking, credential, config)
            if quote.get("cancelled"):
                return {"locator": locator, "status": "cancelled"}
            if confirmation is not None:
                try:
                    accepted = signing.loads(confirmation, salt="iway-cancel", max_age=60)
                except (signing.BadSignature, TypeError):
                    raise AdapterError(
                        "CANCEL_CONFIRMATION_EXPIRED",
                        "Обновите расчёт отмены и подтвердите его заново",
                        category="validation",
                    ) from None
                if accepted != quote:
                    raise AdapterError(
                        "CANCEL_PRICE_CHANGED",
                        "Условия отмены изменились. Требуется новое подтверждение",
                        category="price",
                    )
            elif quote["supplier_cancel_status"] not in ("4", "unapproved_draft"):
                raise AdapterError(
                    "CANCEL_PENALTY_CONFIRMATION_REQUIRED",
                    "Штраф 100% стоимости заказа: "
                    + quote["penalty"]
                    + " "
                    + quote["currency"]
                    + ". Подтвердите расчёт перед отменой",
                    category="validation",
                )
            if quote.get("local_only"):
                booking.update(
                    abandoned_unapproved=True,
                    normalized_status="cancelled",
                    state="abandoned",
                    updated_at=timezone.now().isoformat(),
                    notice="Черновик отозван в CRM. У iway он остаётся неоплаченным и не передаётся в обработку; approve запрещён.",
                )
                self._save(service, snapshot)
                return {
                    "locator": locator,
                    "status": "cancelled",
                    "local_only": True,
                    "notice": booking["notice"],
                    "penalty": {"amount": "0", "currency": quote["currency"]},
                }
            booking["state"] = "cancelling"
            data = self.send_once(
                ctx,
                service,
                snapshot,
                "cancel_sent",
                lambda: self._call(
                    ctx,
                    credential,
                    config,
                    "orders/trips/cancel",
                    "POST",
                    {key: booking[key] for key in ("transaction", "order_id")}
                    | {"user_id": config["user_id"]},
                    mutation=True,
                ),
            )
            result = self._result(service, snapshot, self._one(data))
            result["penalty"] = {"amount": quote["penalty"], "currency": quote["currency"]}
            return result
