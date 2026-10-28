"""Roomlink / A&A XML2 adapter. Supplier identifiers never enter CRM catalogs.

API 4.0.1: multi-rate search/pricing; OrderRequest AccommodationList supports
multiple rooms. Child-age fields are not documented: refuse instead of silently
charging adult prices or losing ages. Deadlines use the hotel's UTC offset when
provided; otherwise retain explicit local date/time without inventing a timezone.
"""

import hashlib
import itertools
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from integrations.aanda_xml import (
    AANDA_AVAILABILITY_MAP,
    AANDA_ORDER_STATUS_MAP,
    AANDA_STARS_MAP,
    attrs,
    child,
    endpoint,
    request,
)
from integrations.adapters import AdapterContext, AdapterError, ProviderAdapter
from integrations.supplier_lock import booking_lease
from suppliers.models import SupplierCredential


def _news(node):
    return [
        {k: item.get(k, "") for k in ("NewsID", "NewsTitle", "NewsInfo", "NewsBegin", "NewsEnd")}
        for item in node.findall("./NewsList/News")
    ]


def _checkin_notice(node):
    fields = {
        "Deposit": ("IsNeeded", "Price", "Currency", "Refundable", "PaymentType"),
        "Checkin": ("Type",),
        "ObtainingKeys": ("Type", "Address", "Time"),
        "ContactInfo": ("Phone", "Email", "AdditionalInfo"),
        "Availability": (
            "AnimalAllowed",
            "ParkingAllowed",
            "TransferAllowed",
            "ForeignerNotAllowed",
            "CleaningFee",
            "Price",
            "Currency",
            "AdditionalInfo",
        ),
    }
    return {
        name: {k: item.get(k, "") for k in keys}
        for name, keys in fields.items()
        if (item := node.find("./CheckinNotice/" + name)) is not None
    }


def _money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return format(amount.normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        raise AdapterError("INVALID_RESPONSE", "A&A: некорректная сумма", category="provider") from None


def _identifier(value, *, supplier=False):
    value = str(value or "")
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise AdapterError(
            "INVALID_RESPONSE" if supplier else "INVALID_REQUEST",
            "A&A: требуется supplier-specific числовой код",
            category="validation",
        )
    return value


def _date(value, *, supplier=False):
    try:
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}" if supplier else r"\d{4}-\d{2}-\d{2}", str(value)):
            raise ValueError
        return (
            datetime.strptime(str(value), "%d.%m.%Y").date() if supplier else date.fromisoformat(str(value))
        )
    except (ValueError, TypeError):
        raise AdapterError(
            "INVALID_RESPONSE" if supplier else "SEARCH_CRITERIA_INVALID",
            "A&A: некорректная дата",
            category="validation",
        ) from None


def _currency(node):
    currency = attrs(node, "Currency")
    code = _identifier(currency.get("Code"), supplier=True)
    name = currency.get("Name", "").upper()
    name = {"RUR": "RUB"}.get(name, name)
    if not re.fullmatch("[A-Z]{3}", name):
        raise AdapterError("INVALID_RESPONSE", "A&A: валюта не получена", category="provider")
    return {"code": code, "name": name}


def _deadline(day, clock, hotel_timezone):
    if not day:
        return None
    # Search combines date + time; Pricing returns separate attributes.
    match = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4})(?:\s*(\d{2}:\d{2}))?", day.strip())
    if not match:
        raise AdapterError("INVALID_RESPONSE", "A&A: некорректный cancellation deadline", category="provider")
    parsed_day = _date(match[1], supplier=True)
    clock = clock or match[2] or ""
    if clock:
        try:
            if not re.fullmatch(r"\d{2}:\d{2}", clock):
                raise ValueError
            datetime.strptime(clock, "%H:%M")
        except ValueError:
            raise AdapterError(
                "INVALID_RESPONSE", "A&A: некорректное время deadline", category="provider"
            ) from None
    result = {
        "date": parsed_day.isoformat(),
        "time": clock,
        "timezone": hotel_timezone or "hotel_local_unknown",
    }
    offset = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", hotel_timezone or "")
    if offset and clock:
        minutes = int(offset[2]) * 60 + int(offset[3] or 0)
        if minutes > 14 * 60:
            raise AdapterError("INVALID_RESPONSE", "A&A: некорректная timezone", category="provider")
        tz = datetime_timezone(timedelta(minutes=minutes if offset[1] == "+" else -minutes))
        result["timestamp"] = datetime.fromisoformat(f"{parsed_day}T{clock}").replace(tzinfo=tz).isoformat()
    return result


class AandaAdapter(ProviderAdapter):
    key = "aanda"
    supported_kinds = ["hotel"]
    requires_revalidation_before_booking = True
    persists_booking_snapshot = True
    requires_passenger_documents = False
    environment_setting = "AANDA_ENVIRONMENT"
    revalidation_ttl = 30

    def hotel_booking_options(self, snapshot):
        return {
            "rooms": [
                {
                    "quantity": 1,
                    "adults": r["adults"],
                    "children": [],
                    "roomName": r["room_name"],
                    "information": [
                        {"title": "Удобства номера", "text": ", ".join(r.get("room_amenities", []))}
                    ]
                    if r.get("room_amenities")
                    else [],
                }
                for r in snapshot["aanda"]["rooms"]
            ],
            "allows_special_requests": True,
        }

    def booking_needs_sync(self, booking):
        return bool(
            booking
            and booking.get("locator")
            and booking.get("state") != "failed"
            and (
                booking.get("state") in ("creating", "created", "unknown")
                or booking.get("normalized_status") in ("pending", "unknown")
                or booking.get("cancel_state") in ("sending", "requested", "unknown")
            )
        )

    def locations(self, ctx, query, *, partial=False):
        """Country-labelled suggestions may be partial; unselected text search stays exhaustive."""
        credential, config = self._config(ctx)
        key = f"aanda:cities:{ctx.tenant_id}:{ctx.supplier_id}:{credential.id}:{ctx.environment}:{config.get('language', 'ru').lower()}"
        needle = self._location_text(query)
        cities = cache.get(key)
        if cities is None:
            countries = cache.get(key + ":countries")
            if countries is None:
                response = request(
                    ctx, "country_list", "CountryListRequest", ET.Element("CountryListRequest"), config
                )
                countries = [
                    {"Code": _identifier(n.get("Code")), "Name": n.get("Name", "")}
                    for n in response.findall("Country")
                ]
                cache.set(key + ":countries", countries, 86400)
            cities = []
            for country in countries:
                country_code = country["Code"]
                country_cities = cache.get(key + ":" + country_code)
                if country_cities is None:
                    response = request(
                        ctx,
                        "city_list",
                        "CityListRequest",
                        ET.Element("CityListRequest", {"CountryCode": country_code}),
                        config,
                    )
                    country_cities = [
                        {
                            "city_code": _identifier(n.get("Code")),
                            "country_code": country_code,
                            "name": n.get("Name", ""),
                            "country": country["Name"],
                        }
                        for n in response.findall("./Country/City")
                    ]
                    cache.set(key + ":" + country_code, country_cities, 86400)
                cities.extend(country_cities)
                matches = [n for n in cities if needle in self._location_text(n["name"])]
                if partial and matches:
                    # An explicit selection carries this provider's city code and country.
                    # Do not cache an incomplete index as a globally unique text match.
                    return matches[:10]
            cache.set(key, cities, 86400)
        return [n for n in cities if needle in self._location_text(n["name"])]

    @staticmethod
    def _location_text(value):
        return " ".join(str(value).casefold().replace("ё", "е").replace("-", " ").split())

    def _secrets(self, credential):
        try:
            config = json.loads(credential.encrypted_secrets)
            if not isinstance(config, dict) or not all(
                isinstance(config.get(k), str) and config[k].strip()
                for k in ("buyer_id", "user_id", "password")
            ):
                raise ValueError
            if config.get("language", "ru").lower() not in ("ru", "en"):
                raise ValueError
        except (ValueError, TypeError, RuntimeError):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "A&A: заполните BuyerId, UserId, Password и Language ru/en",
                category="configuration",
            ) from None
        return config

    def _config(self, ctx):
        environment = self.configured_environment()
        if ctx.environment and ctx.environment != environment:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "A&A: окружение snapshot не соответствует серверу",
                category="configuration",
            )
        credential = SupplierCredential.objects.filter(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            supplier__tenant_id=ctx.tenant_id,
            supplier__archived_at__isnull=True,
            provider_adapter=self.key,
            environment=environment,
            status="active",
            archived_at__isnull=True,
        ).first()
        if credential is None:
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "A&A: нет активных credentials в этом окружении",
                category="configuration",
            )
        ctx.environment = environment
        endpoint(environment)
        return credential, self._secrets(credential)

    def verify_credentials(self, credential):
        if (
            credential.provider_adapter != self.key
            or credential.environment != self.configured_environment()
            or credential.tenant_id != credential.supplier.tenant_id
        ):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED", "A&A: неверное окружение credentials", category="configuration"
            )
        config = self._secrets(credential)
        ctx = AdapterContext(
            tenant_id=credential.tenant_id,
            supplier_id=credential.supplier_id,
            environment=credential.environment,
        )
        request(ctx, "verify_credentials", "CurrencyListRequest", ET.Element("CurrencyListRequest"), config)
        return "A&A: авторизация подтверждена (CurrencyListRequest)"

    def _rooms(self, criteria):
        if criteria.get("children"):
            raise AdapterError(
                "NOT_SUPPORTED",
                "A&A: детские тарифы требуют документированного протокола возраста",
                category="validation",
            )
        rooms = criteria.get("rooms")
        if not isinstance(rooms, list):
            try:
                count = int(rooms or 1)
                guests = int(criteria.get("adults", criteria.get("guests", 1)))
                if count != 1:
                    raise ValueError
            except (ValueError, TypeError):
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID",
                    "A&A: укажите список rooms с числом взрослых для каждой комнаты",
                    category="validation",
                ) from None
            rooms = [{"adults": guests, "children": criteria.get("children", [])}]
        if not 1 <= len(rooms) <= 8:
            raise AdapterError("SEARCH_CRITERIA_INVALID", "A&A: допустимо 1–8 комнат", category="validation")
        result = []
        for room in rooms:
            if not isinstance(room, dict) or room.get("quantity", 1) != 1:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID",
                    "A&A: каждая комната должна иметь quantity=1",
                    category="validation",
                )
            children = room.get("children") or room.get("children_ages") or room.get("childrenAges") or []
            if children:
                raise AdapterError(
                    "NOT_SUPPORTED",
                    "A&A XML2: протокол возраста детей не документирован; требуется согласование с поставщиком",
                    category="validation",
                )
            try:
                adults = room.get("adults", 1)
                if not isinstance(adults, int) or isinstance(adults, bool) or not 1 <= adults <= 4:
                    raise ValueError
            except (ValueError, TypeError):
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID", "A&A: неверное число гостей", category="validation"
                ) from None
            result.append({"adults": adults, "children_ages": []})
        return result

    def search(self, ctx, kind, criteria):
        if kind != "hotel":
            raise AdapterError("NOT_SUPPORTED", "A&A поддерживает только гостиницы", category="validation")
        credential, config = self._config(ctx)
        spec = criteria.get("aanda") or {}
        start, end = _date(criteria.get("check_in")), _date(criteria.get("check_out"))
        if start < timezone.localdate() or end <= start:
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "A&A: проверьте даты проживания", category="validation"
            )
        destination = {}
        if spec.get("hotel_codes"):
            destination["Hotels"] = ",".join(_identifier(code) for code in spec["hotel_codes"])
        elif spec.get("city_code"):
            destination["City"] = _identifier(spec["city_code"])
        else:
            location = str(criteria.get("location", "")).strip()
            matches = (
                [
                    city
                    for city in self.locations(ctx, location)
                    if self._location_text(city["name"]) == self._location_text(location)
                ]
                if len(location) >= 2 and not location.isdigit()
                else []
            )
            if len(matches) != 1:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID",
                    "A&A: выберите однозначный город или передайте aanda.city_code / hotel_codes",
                    category="validation",
                )
            destination["City"] = matches[0]["city_code"]
        allocations = self._rooms(criteria)
        pools = []
        for index, allocation in enumerate(allocations):
            root = ET.Element(
                "HotelSearchRequest",
                {
                    **destination,
                    "ArrivalDate": start.strftime("%d.%m.%Y"),
                    "DepartureDate": end.strftime("%d.%m.%Y"),
                    "NumberOfGuests": str(allocation["adults"]),
                    "IsMultiRate": "1",
                    "FreeSaleOnly": "2",
                },
            )
            response = request(ctx, "hotel_search", "HotelSearchRequest", root, config)
            if response.get("ArrivalDate") and _date(response.get("ArrivalDate"), supplier=True) != start:
                raise AdapterError("INVALID_RESPONSE", "A&A: неверные даты результата", category="provider")
            pool = []
            for hotel in response.findall("./AvailableHotelsList/Hotel"):
                for detail in hotel.findall("./HotelOfferList/HotelOfferDetail"):
                    for rate in detail.findall("./Rates/Rate"):
                        payments = rate.findall("./WhereToPay") or rate.findall("./WhereToPayList/WhereToPay")
                        for payment in payments:
                            room = self._normalize_room(
                                hotel, detail, rate, payment, start, end, allocation, pricing=False
                            )
                            room["room_ref"] = f"room:{index}"
                            pool.append(room)
            pools.append(pool)
        # Only combine rooms in the same supplier hotel and settlement currency.
        hotels = set(r["hotel_code"] for r in pools[0])
        for pool in pools[1:]:
            hotels.intersection_update(r["hotel_code"] for r in pool)
        offers = []
        for hotel_code in sorted(hotels):
            groups = [[r for r in pool if r["hotel_code"] == hotel_code] for pool in pools]
            if len(allocations) > 1 and self._combination_count(groups) > 4096:
                raise AdapterError(
                    "SEARCH_CRITERIA_INVALID",
                    "A&A: слишком много комбинаций; уточните отель или количество комнат",
                    category="validation",
                )
            for combination in itertools.product(*groups):
                if len({(r["currency"]["code"], r["where_to_pay"]) for r in combination}) != 1:
                    continue
                offers.append(self._snapshot(ctx, credential, list(combination)))
        return offers

    @staticmethod
    def _combination_count(groups):
        count = 1
        for group in groups:
            count *= len(group)
        return count

    def _normalize_room(self, *args, **kwargs):
        try:
            return self._parse_room(*args, **kwargs)
        except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
            raise AdapterError(
                "INVALID_RESPONSE", "A&A: некорректные данные тарифа", category="provider"
            ) from None

    def _parse_room(self, hotel, detail, rate, payment, start, end, allocation, *, pricing):
        pax = allocation["adults"]
        values = rate.attrib
        rate_id = values.get("RateID") if pricing else values.get("ID")
        currency = _currency(hotel)
        availability = attrs(rate, "Availability")
        raw_availability = availability.get("Code", "")
        meal = attrs(rate, "Meal")
        # Additional meals (Code 2) are optional and NOT purchased implicitly.
        meal_code = "1" if meal.get("Code") == "1" else "0"
        total = values.get(f"Price{pax}Pax") if pricing else values.get("TotalPrice")
        penalty = values.get(f"PenaltySize{pax}Pax") if pricing else values.get("PenaltySize")
        tz = hotel.get("Timezone", "")
        result = {
            "hotel_code": _identifier(hotel.get("Code"), supplier=True),
            "hotel_name": hotel.get("Name", ""),
            "address": hotel.get("Address", ""),
            "hotel_description": hotel.get("Description", ""),
            "hotel_amenities": [
                n.get("Name", "") for n in hotel.findall("./HotelAmenities/HotelAmenity") if n.get("Name")
            ],
            "room_amenities": [n.get("Name", "") for n in detail.findall(".//RoomAmenity") if n.get("Name")],
            "room_size": detail.get("RoomSize", ""),
            "news": _news(hotel),
            "city": {k: v for k, v in attrs(hotel, "City").items() if k in ("Code", "Name")},
            "country": {k: v for k, v in attrs(hotel, "Country").items() if k in ("Code", "Name")},
            "stars": {k: v for k, v in attrs(hotel, "Stars").items() if k in ("Code", "Name")},
            "product_code": _identifier(detail.get("Code"), supplier=True),
            "room_name": detail.get("RoomName", ""),
            "rate_id": _identifier(rate_id, supplier=True),
            "rate_name": values.get("RateName" if pricing else "Name", ""),
            "rate_description": values.get("RateDescription" if pricing else "Description", ""),
            "adults": pax,
            "children_ages": [],
            "check_in": start.isoformat(),
            "check_out": end.isoformat(),
            "arrival_time": hotel.get("CheckInTime", ""),
            "departure_time": hotel.get("CheckOutTime", ""),
            "currency": currency,
            "total_price": _money(total),
            "commission": _money(
                values.get(f"TravelAgencyCommission{pax}Pax" if pricing else "TravelAgencyCommission", "0")
            ),
            "penalty": _money(penalty) if penalty not in (None, "") else None,
            "deadline": _deadline(values.get("DeadlineDate", ""), values.get("DeadlineTime", ""), tz),
            "penalty_info": values.get("PenaltyInfo", ""),
            "reservation_lifetime": _deadline(
                values.get("ReservationLifetimeDate", ""), values.get("ReservationLifetimeTime", ""), tz
            ),
            "raw_availability": raw_availability,
            "availability": AANDA_AVAILABILITY_MAP.get(raw_availability, "unknown"),
            "quota": availability.get("Rooms", ""),
            "where_to_pay": _identifier(payment.get("Code"), supplier=True),
            "meal": {
                k: meal.get(k, "") for k in ("Code", "TypeCode", "TypeName", "CategoryCode", "CategoryName")
            },
            "meal_code": meal_code,
            "meal_price": meal.get("Price", ""),
            "payment_name": payment.get("Name", ""),
            "group_info": {
                k: v
                for k, v in attrs(hotel, "GroupInfo").items()
                if k in ("GroupQtyFrom", "GroupType", "GroupName", "GroupNote", "GroupRule")
            },
        }
        if values.get("NumberOfGuests") and int(values["NumberOfGuests"]) != pax:
            raise AdapterError(
                "INVALID_RESPONSE",
                "A&A: число гостей результата не соответствует запросу",
                category="provider",
            )
        return result

    def _snapshot(self, ctx, credential, rooms):
        first = rooms[0]
        identity = [
            {
                k: r[k]
                for k in (
                    "hotel_code",
                    "product_code",
                    "rate_id",
                    "meal_code",
                    "where_to_pay",
                    "currency",
                    "adults",
                    "check_in",
                    "check_out",
                )
            }
            for r in rooms
        ]
        external_key = "AA-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        availability = (
            "available"
            if all(r["availability"] == "available" for r in rooms)
            else "on_request"
            if all(r["availability"] in ("available", "on_request") for r in rooms)
            else "unknown"
        )
        # Several rooms may consume the same allotment. Never promise instant
        # confirmation when the combination exceeds the returned quota.
        for room in rooms:
            required = sum(r["product_code"] == room["product_code"] for r in rooms)
            if room["quota"].isdigit() and int(room["quota"]) < required and availability == "available":
                availability = "on_request"
        snapshot = {
            "provider_adapter": self.key,
            "external_key": external_key,
            "kind": "hotel",
            "price": {
                "amount": str(sum((Decimal(r["total_price"]) for r in rooms), Decimal(0))),
                "currency": first["currency"]["name"],
            },
            "availability": availability,
            "expires_at": (timezone.now() + timedelta(minutes=30)).isoformat(),
            "itinerary": {
                "property_name": first["hotel_name"],
                "city": first["city"].get("Name", ""),
                "country": first["country"].get("Name", ""),
                "stars": AANDA_STARS_MAP.get(first["stars"].get("Code")),
                "check_in": first["check_in"],
                "check_out": first["check_out"],
                "room": "; ".join(r["room_name"] for r in rooms),
                "meal_plan": first["meal"].get("TypeName", "")
                if first["meal_code"] == "1"
                else "Питание не включено",
                "check_in_time": first["arrival_time"],
                "check_out_time": first["departure_time"],
                "address": first.get("address", ""),
                "area": first.get("room_size") or None,
                "rooms_count": len(rooms),
                "max_occupancy": sum(r["adults"] for r in rooms),
            },
            "aanda": {
                "environment": ctx.environment,
                "credential_id": str(credential.id),
                "rooms": rooms,
                "terms_source": "search",
            },
        }
        snapshot["fare"] = self.fare_rules(ctx, snapshot)
        return snapshot

    def _validate_snapshot(self, ctx, snapshot):
        if snapshot.get("provider_adapter") != self.key or not (snapshot.get("aanda") or {}).get("rooms"):
            raise AdapterError(
                "INVALID_REQUEST",
                "A&A: snapshot другого поставщика или отсутствуют комнаты",
                category="validation",
            )
        ctx.environment = snapshot["aanda"].get("environment", "")
        credential, config = self._config(ctx)
        if snapshot["aanda"].get("credential_id") != str(credential.id):
            raise AdapterError(
                "PROVIDER_NOT_CONFIGURED",
                "A&A: credentials snapshot не соответствуют выбранному supplier",
                category="configuration",
            )
        return credential, config

    def fare_rules(self, ctx, offer_snapshot):
        rooms = offer_snapshot.get("aanda", {}).get("rooms", [])
        cancellations = [
            {
                "room_ref": r.get("room_ref"),
                "penalty": r.get("penalty"),
                "currency": r.get("currency", {}).get("name"),
                "deadline": r.get("deadline"),
                "information": r.get("penalty_info", ""),
            }
            for r in rooms
        ]
        texts = []
        for rule in cancellations:
            deadline = rule["deadline"] or {}
            when = deadline.get("timestamp") or " ".join(
                filter(None, (deadline.get("date"), deadline.get("time"), deadline.get("timezone")))
            )
            texts.append(
                "; ".join(
                    filter(
                        None,
                        (
                            f"{rule['room_ref']}: штраф {rule['penalty']} {rule['currency']}"
                            if rule["penalty"] is not None
                            else "Размер штрафа не передан",
                            f"С {when}" if when else "",
                            rule["information"],
                        ),
                    )
                )
            )
        return {
            "cancellation_details": cancellations,
            "cancellation_rules": "\n".join(texts),
            "name": "; ".join(r["rate_name"] for r in rooms),
            "confirmation_mode": "ONLINE"
            if offer_snapshot.get("availability") == "available"
            else "ON_REQUEST",
            "rooms": self.hotel_booking_options(offer_snapshot)["rooms"],
            "isRemarkFreeFormAllowed": True,
            "information": [
                {
                    "title": n.get("NewsTitle", ""),
                    "text": n.get("NewsInfo", ""),
                    "from": n.get("NewsBegin", ""),
                    "to": n.get("NewsEnd", ""),
                }
                for n in (rooms[0].get("news", []) if rooms else [])
            ]
            + [
                {"title": "Описание тарифа", "text": r["rate_description"]}
                for r in rooms
                if r.get("rate_description")
            ],
            "pay_at_hotel": True if rooms and all(r["where_to_pay"] == "2" for r in rooms) else None,
            "meal_information": [
                {
                    "included": r["meal_code"] == "1",
                    "name": r["meal"].get("TypeName", ""),
                    "category": r["meal"].get("CategoryName", ""),
                    "price_per_person": r.get("meal_price", ""),
                    "currency": r["currency"]["name"],
                }
                for r in rooms
            ],
            "payment_rules": [
                r.get("where_to_pay") for r in offer_snapshot.get("aanda", {}).get("rooms", [])
            ],
            "availability": offer_snapshot.get("availability"),
        }

    @staticmethod
    def _terms(snapshot):
        fields = (
            "hotel_code",
            "product_code",
            "rate_id",
            "adults",
            "children_ages",
            "check_in",
            "check_out",
            "arrival_time",
            "departure_time",
            "currency",
            "penalty",
            "deadline",
            "penalty_info",
            "reservation_lifetime",
            "raw_availability",
            "where_to_pay",
            "meal",
            "meal_code",
            "rate_description",
            "group_info",
            "news",
        )
        return [{k: r.get(k) for k in fields} for r in snapshot["aanda"]["rooms"]]

    def revalidate(self, ctx, offer_snapshot):
        credential, config = self._validate_snapshot(ctx, offer_snapshot)
        rooms = []
        for previous in offer_snapshot["aanda"]["rooms"]:
            root = ET.Element(
                "HotelPricingRequest",
                {
                    "Hotel": previous["hotel_code"],
                    "ProductCode": previous["product_code"],
                    "Currency": previous["currency"]["code"],
                    "WhereToPay": previous["where_to_pay"],
                    "ArrivalDate": _date(previous["check_in"]).strftime("%d.%m.%Y"),
                    "DepartureDate": _date(previous["check_out"]).strftime("%d.%m.%Y"),
                    "ArrivalTime": previous["arrival_time"],
                    "DepartureTime": previous["departure_time"],
                    "NumberOfGuests": str(previous["adults"]),
                    "IsMultiRate": "1",
                },
            )
            response = request(ctx, "hotel_pricing", "HotelPricingRequest", root, config)
            if response.get("Code") != previous["hotel_code"]:
                raise AdapterError("RATE_UNAVAILABLE", "A&A: изменился отель", category="availability")
            for attr, expected in (("StartDate", previous["check_in"]), ("EndDate", previous["check_out"])):
                if response.get(attr) and _date(response.get(attr), supplier=True).isoformat() != expected:
                    raise AdapterError(
                        "AVAILABILITY_CONFLICT", "A&A: изменились даты проживания", category="availability"
                    )
            candidates = []
            for detail in response.findall("./HotelPricingList/HotelPricingDetail"):
                if detail.get("Code") != previous["product_code"]:
                    continue
                for rate in detail.findall("./Rates/Rate"):
                    if rate.get("RateID") != previous["rate_id"]:
                        continue
                    for payment in rate.findall("./WhereToPayList/WhereToPay") + rate.findall("./WhereToPay"):
                        if payment.get("Code") == previous["where_to_pay"]:
                            candidates.append(
                                self._normalize_room(
                                    response,
                                    detail,
                                    rate,
                                    payment,
                                    _date(previous["check_in"]),
                                    _date(previous["check_out"]),
                                    previous,
                                    pricing=True,
                                )
                            )
            if len(candidates) != 1:
                raise AdapterError(
                    "RATE_UNAVAILABLE", "A&A: выбранный room/rate/payment недоступен", category="availability"
                )
            room = candidates[0]
            room["room_ref"] = previous["room_ref"]
            # Pricing omits search-only hotel classification/group metadata.
            for key in (
                "stars",
                "group_info",
                "news",
                "address",
                "hotel_description",
                "hotel_amenities",
                "room_size",
            ):
                if not room[key]:
                    room[key] = previous.get(key, {})
            rooms.append(room)
        updated = self._snapshot(ctx, credential, rooms)
        for key in ("special_requests", "main_guest"):
            if key in offer_snapshot:
                updated[key] = offer_snapshot[key]
        if updated["availability"] not in ("available", "on_request"):
            raise AdapterError(
                "RATE_UNAVAILABLE", "A&A: доступность не подтверждена", category="availability"
            )
        price_changed = (
            Decimal(updated["price"]["amount"]) != Decimal(offer_snapshot["price"]["amount"])
            or updated["price"]["currency"] != offer_snapshot["price"]["currency"]
        )
        terms_changed = (
            self._terms(updated) != self._terms(offer_snapshot)
            or updated["availability"] != offer_snapshot["availability"]
        )
        updated["aanda"].update(terms_source="pricing", revalidated_at=timezone.now().isoformat())
        updated["aanda_revalidation"] = signing.dumps(self._proof(ctx, updated), salt="aanda.revalidation")
        return {
            "status": "price_changed" if price_changed else "valid",
            "price": updated["price"],
            "terms_changed": terms_changed,
            "fare_rules": updated["fare"],
            "snapshot": updated,
        }

    def _proof(self, ctx, snapshot):
        data = {k: snapshot.get(k) for k in ("aanda", "price", "availability", "external_key", "expires_at")}
        return {
            "tenant": str(ctx.tenant_id),
            "supplier": str(ctx.supplier_id),
            "hash": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
        }

    def _booking_validation(self, ctx, snapshot):
        self._validate_snapshot(ctx, snapshot)
        try:
            proof = signing.loads(
                snapshot.get("aanda_revalidation", ""),
                salt="aanda.revalidation",
                max_age=self.revalidation_ttl,
            )
            if proof == self._proof(ctx, snapshot) and snapshot["aanda"].get("terms_source") == "pricing":
                return {"status": "valid", "terms_changed": False, "snapshot": snapshot}
        except signing.BadSignature:
            return self.revalidate(ctx, snapshot)
        return self.revalidate(ctx, snapshot)

    def _guests(self, snapshot, passengers):
        rooms = snapshot["aanda"]["rooms"]
        if len(passengers) != sum(r["adults"] for r in rooms):
            raise AdapterError(
                "GUEST_ROOM_MISMATCH", "A&A: количество гостей не соответствует поиску", category="validation"
            )
        groups = []
        for room in rooms:
            guests = (
                [p for p in passengers if str(p.get("room_ref", "")) == room["room_ref"]]
                if len(rooms) > 1
                else passengers
            )
            if len(guests) != room["adults"]:
                raise AdapterError(
                    "GUEST_ROOM_MISMATCH",
                    "A&A: назначьте каждого гостя в room_ref выбранной комнаты",
                    category="validation",
                )
            persons = []
            for guest in guests:
                if guest.get("birth_date"):
                    birth = _date(guest["birth_date"])
                    arrival = _date(room["check_in"])
                    age = (
                        arrival.year - birth.year - ((arrival.month, arrival.day) < (birth.month, birth.day))
                    )
                    if age < 18:
                        raise AdapterError(
                            "NOT_SUPPORTED",
                            "A&A: детское размещение требует документированного протокола",
                            category="validation",
                        )
                last = guest.get("latin_surname") or guest.get("surname")
                first = guest.get("latin_given_name") or guest.get("given_name")
                if not last or not first or len(last) > 100 or len(first) > 100:
                    raise AdapterError(
                        "INVALID_REQUEST", "A&A: заполните имя и фамилию гостя", category="validation"
                    )
                persons.append({"LastName": last, "FirstName": first})
            groups.append(persons)
        return groups

    def _order_xml(self, snapshot, groups, reference, *, order_id="", cancel=False, accommodations=None):
        root = ET.Element("OrderRequest", {"Id": str(order_id), "ReferenceNumber": reference})
        rooms = snapshot["aanda"]["rooms"]
        child(root, "Currency", Code=rooms[0]["currency"]["code"])
        child(root, "WhereToPay", Code=rooms[0]["where_to_pay"])
        listing = child(root, "AccommodationList")
        for index, room in enumerate(rooms):
            accommodation_id = accommodations[index]["id"] if accommodations else ""
            node = child(
                listing,
                "Accommodation",
                Id=accommodation_id,
                ArrivalDate=_date(room["check_in"]).strftime("%d.%m.%Y"),
                DepartureDate=_date(room["check_out"]).strftime("%d.%m.%Y"),
                ArrivalTime=room["arrival_time"],
                DepartureTime=room["departure_time"],
                NumberOfRooms="1",
                NumberOfGuests=room["adults"],
            )
            remark = snapshot.get("special_requests", "")
            if (
                not isinstance(remark, str)
                or len(remark) > 2000
                or any(ord(c) < 32 and c not in "\n\r\t" for c in remark)
            ):
                raise AdapterError("INVALID_REQUEST", "A&A: некорректные пожелания", category="validation")
            if remark:
                node.set("AdditionalInfo", remark)
            child(node, "ToBeCancelled", Code="1" if cancel else "2")
            child(node, "ToBeChanged", Code="1")
            child(node, "Hotel", Code=room["hotel_code"])
            product = child(node, "Product", Code=room["product_code"], RateID=room["rate_id"])
            child(product, "Allotment", Code="0")
            child(node, "Meal", Code=room["meal_code"])
            persons = child(node, "Persons")
            for guest in groups[index] if groups else []:
                child(persons, "Person", **guest)
        return root

    @staticmethod
    def _save(service, snapshot):
        service.provider_snapshot = snapshot
        service.save(update_fields=["provider_snapshot", "updated_at"])

    def book(self, ctx, booking_request):
        from services.models import OrderService

        service_id = booking_request.get("service_id")
        service = OrderService.objects.filter(
            pk=service_id,
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            source="api",
            provider_snapshot__provider_adapter=self.key,
        ).first()
        if service is None:
            raise AdapterError(
                "BOOKING_CONTEXT_REQUIRED",
                "A&A: требуется услуга выбранного supplier в этом tenant",
                category="configuration",
            )
        ctx.order_id = service.order_id
        with booking_lease(service):
            service.refresh_from_db()
            snapshot = deepcopy(service.provider_snapshot)
            self._validate_snapshot(ctx, snapshot)
            existing = snapshot.get("aanda_booking")
            if existing:
                if existing.get("state") == "complete" and existing.get("normalized_status") in (
                    "booked",
                    "pending",
                ):
                    return self._result(snapshot)
                raise AdapterError(
                    "BOOKING_UNKNOWN",
                    "A&A: выполните status inquiry; повторный OrderRequest запрещён",
                    category="booking_unknown",
                )
            if service.status in ("booked", "confirmed", "issued", "cancelled"):
                raise AdapterError("BOOKING_REJECTED", "A&A: услуга уже обработана", category="booking")
            validation = self._booking_validation(ctx, snapshot)
            if validation["status"] != "valid":
                raise AdapterError(
                    "PRICE_CHANGED", "A&A: цена изменилась; повторите preflight", category="price"
                )
            if validation.get("terms_changed"):
                raise AdapterError(
                    "RATE_CHANGED", "A&A: условия изменились; повторите preflight", category="availability"
                )
            snapshot = validation["snapshot"]
            groups = self._guests(snapshot, booking_request.get("passengers") or [])
            reference = "AA" + hashlib.sha256(f"{ctx.tenant_id}:{service.pk}".encode()).hexdigest()[:30]
            root = self._order_xml(snapshot, groups, reference)
            _, config = self._config(ctx)
            with transaction.atomic():
                locked = OrderService.objects.select_for_update().get(pk=service.pk)
                if (locked.provider_snapshot or {}).get("aanda_booking"):
                    raise AdapterError(
                        "BOOKING_UNKNOWN", "A&A: операция уже сохранена", category="booking_unknown"
                    )
                snapshot["aanda_booking"] = {
                    "locator": reference,
                    "reference_number": reference,
                    "state": "creating",
                    "normalized_status": "unknown",
                    "created_at": timezone.now().isoformat(),
                }
                self._save(locked, snapshot)
            try:
                response = request(ctx, "order_create", "OrderRequest", root, config, mutation=True)
                order_id = attrs(response, "Success").get("Id")
                if not order_id or not order_id.isdigit():
                    raise AdapterError(
                        "BOOKING_UNKNOWN",
                        "A&A: OrderId не получен; выполните status inquiry",
                        category="booking_unknown",
                    )
                snapshot["aanda_booking"].update(order_id=order_id, state="created")
                self._save(service, snapshot)
                # Success Id acknowledges creation, not supplier confirmation.
                return self._retrieve_locked(ctx, service, snapshot, config)
            except AdapterError as exc:
                snapshot["aanda_booking"].update(
                    state="unknown"
                    if exc.category == "booking_unknown" or snapshot["aanda_booking"].get("order_id")
                    else "failed",
                    error_code=exc.code,
                )
                self._save(service, snapshot)
                if snapshot["aanda_booking"].get("order_id") and exc.category != "booking_unknown":
                    raise AdapterError(
                        "BOOKING_UNKNOWN",
                        "A&A: заказ создан, его статус требует сверки",
                        category="booking_unknown",
                    ) from None
                raise

    def _stored(self, ctx, locator):
        from services.models import OrderService

        qs = OrderService.objects.filter(
            tenant_id=ctx.tenant_id,
            supplier_id=ctx.supplier_id,
            source="api",
            provider_snapshot__provider_adapter=self.key,
        )
        service = qs.filter(provider_snapshot__aanda_booking__locator=locator).first()
        if service is None:
            service = qs.filter(provider_snapshot__aanda_booking__order_id=str(locator)).first()
        if service is None:
            raise AdapterError(
                "BOOKING_NOT_FOUND", "A&A: бронь этого supplier не найдена в tenant", category="sync"
            )
        return service

    def retrieve_booking(self, ctx, locator):
        service = self._stored(ctx, locator)
        with booking_lease(service):
            service.refresh_from_db()
            snapshot = deepcopy(service.provider_snapshot)
            _, config = self._validate_snapshot(ctx, snapshot)
            ctx.order_id = service.order_id
            return self._retrieve_locked(ctx, service, snapshot, config)

    def _order_info(self, ctx, booking, config):
        root = ET.Element(
            "OrderInfoRequest",
            {"Id": str(booking.get("order_id", "")), "ReferenceNumber": booking["reference_number"]},
        )
        return request(ctx, "order_info", "OrderInfoRequest", root, config)

    def _retrieve_locked(self, ctx, service, snapshot, config, *, response=None):
        booking = snapshot["aanda_booking"]
        if response is None:
            response = self._order_info(ctx, booking, config)
        order_id = response.get("Id", "")
        if not order_id.isdigit() or booking.get("order_id") and order_id != str(booking["order_id"]):
            raise AdapterError("INVALID_RESPONSE", "A&A: OrderId не соответствует брони", category="sync")
        if response.get("ReferenceNumber") != booking["reference_number"]:
            raise AdapterError(
                "INVALID_RESPONSE", "A&A: ReferenceNumber не соответствует брони", category="sync"
            )
        raw_status = attrs(response, "Status").get("Code", "")
        previous_items = {item["id"]: item for item in booking.get("items", [])}
        items = []
        for accommodation in response.findall("./AccommodationList/Accommodation"):
            items.append(
                {
                    "id": _identifier(accommodation.get("Id"), supplier=True),
                    "hotel_code": attrs(accommodation, "Hotel").get("Code"),
                    "product_code": attrs(accommodation, "Product").get("Code"),
                    "rate_id": attrs(accommodation, "Product").get("RateID"),
                    "status": attrs(accommodation, "Status").get("Code", raw_status),
                    "allow_cancel": attrs(accommodation, "AllowCancel").get("Code") == "1",
                    "confirmation_number": accommodation.get("ConfirmationNumber")
                    or previous_items.get(accommodation.get("Id"), {}).get("confirmation_number", ""),
                    "supplier_info": accommodation.get("SupplierInfo", ""),
                    "additional_info": accommodation.get("AdditionalInfo", ""),
                    "penalty_note": accommodation.get("Note", ""),
                    "raw_status_name": attrs(accommodation, "Status").get("Name", ""),
                    "penalty": _money(accommodation.get("Penalty")) if accommodation.get("Penalty") else None,
                    "possible_penalty": _money(accommodation.get("PossiblePenaltySize"))
                    if accommodation.get("PossiblePenaltySize")
                    else None,
                    "deadline_time": accommodation.get("DeadlineTime", response.get("DeadlineTime", "")),
                    "deadline_date": _date(accommodation.get("DeadlineDate"), supplier=True).isoformat()
                    if accommodation.get("DeadlineDate")
                    else "",
                }
            )
        if not items:
            raise AdapterError("INVALID_RESPONSE", "A&A: заказ не содержит размещений", category="sync")
        expected_rooms = snapshot["aanda"]["rooms"]
        if len(items) != len(expected_rooms) or sorted(
            (i["hotel_code"], i["product_code"], i["rate_id"]) for i in items
        ) != sorted((r["hotel_code"], r["product_code"], r["rate_id"]) for r in expected_rooms):
            raise AdapterError(
                "INVALID_RESPONSE", "A&A: состав заказа не соответствует выбранным комнатам", category="sync"
            )
        status = AANDA_ORDER_STATUS_MAP.get(raw_status, "pending")
        item_statuses = [AANDA_ORDER_STATUS_MAP.get(i["status"], "pending") for i in items]
        if status == "booked" and any(s != "booked" for s in item_statuses):
            status = "pending"
        if status == "cancelled" and any(s != "cancelled" for s in item_statuses):
            status = "pending"
        booking.update(
            order_id=order_id,
            state="complete",
            raw_status=raw_status,
            raw_status_name=attrs(response, "Status").get("Name", ""),
            news=_news(response),
            checkin_notice=_checkin_notice(response),
            normalized_status=status,
            registration_date=response.get("RegistrationDate", ""),
            deadline=_deadline(
                response.get("DeadlineDate", ""),
                response.get("DeadlineTime", ""),
                snapshot["aanda"]["rooms"][0].get("deadline", {}).get("timezone", "")
                if snapshot["aanda"]["rooms"][0].get("deadline")
                else "",
            ),
            items=items,
            updated_at=timezone.now().isoformat(),
        )
        if response.get("Price"):
            actual_price = {"amount": _money(response.get("Price")), "currency": _currency(response)["name"]}
            booking["actual_price"] = actual_price
            booking["price_discrepancy"] = (
                Decimal(actual_price["amount"]) != Decimal(snapshot["price"]["amount"])
                or actual_price["currency"] != snapshot["price"]["currency"]
            )
            if booking["price_discrepancy"]:
                from integrations.models import IntegrationIncident

                # OrderRequest has no documented expected-price constraint.
                # Keep the real booking/status and alert an operator; never
                # automatically cancel or send another financial request.
                IntegrationIncident.objects.get_or_create(
                    tenant_id=ctx.tenant_id,
                    supplier_id=ctx.supplier_id,
                    service=service,
                    error_code="PRICE_CHANGED",
                    status="open",
                    defaults={
                        "provider_adapter": self.key,
                        "order_id": service.order_id,
                        "operation": "order_info",
                        "severity": "high",
                        "correlation_id": ctx.correlation_id,
                        "sanitized_error": "A&A: цена созданного заказа отличается от подписанной Pricing; требуется проверка оператора",
                    },
                )
        if status == "cancelled":
            booking["cancel_state"] = "complete"
        self._save(service, snapshot)
        return self._result(snapshot)

    @staticmethod
    def _result(snapshot):
        booking = snapshot["aanda_booking"]
        return {
            "locator": booking["locator"],
            "status": booking.get("normalized_status", "pending"),
            "supplier_status": booking.get("raw_status", ""),
            "provider_snapshot": snapshot,
        }

    def cancel(self, ctx, locator):
        service = self._stored(ctx, locator)
        with booking_lease(service):
            service.refresh_from_db()
            snapshot = deepcopy(service.provider_snapshot)
            _, config = self._validate_snapshot(ctx, snapshot)
            ctx.order_id = service.order_id
            response = self._order_info(ctx, snapshot["aanda_booking"], config)
            result = self._retrieve_locked(ctx, service, snapshot, config, response=response)
            booking = snapshot["aanda_booking"]
            if result["status"] == "cancelled":
                return result
            if booking.get("cancel_state") in ("sending", "requested", "unknown", "complete"):
                raise AdapterError(
                    "CANCELLATION_UNKNOWN",
                    "A&A: запрос отмены уже отправлен; выполните status inquiry",
                    category="booking_unknown",
                )
            if booking.get("raw_status") not in ("18", "21", "25", "35", "65") or not all(
                i["allow_cancel"] for i in booking["items"]
            ):
                raise AdapterError(
                    "CANCELLATION_FAILED", "A&A: поставщик запрещает отмену", category="validation"
                )
            if any(i["penalty"] is None or i["possible_penalty"] is None for i in booking["items"]):
                raise AdapterError(
                    "CANCELLATION_FAILED",
                    "A&A: размер штрафа не подтверждён свежим OrderInfo",
                    category="validation",
                )
            # Preserve supplier accommodation order even if CRM rooms were sorted differently.
            accommodations = []
            remaining = list(booking["items"])
            for room in snapshot["aanda"]["rooms"]:
                item = next(
                    i
                    for i in remaining
                    if (i["hotel_code"], i["product_code"], i["rate_id"])
                    == (room["hotel_code"], room["product_code"], room["rate_id"])
                )
                remaining.remove(item)
                accommodations.append(item)
            # Guest names remain volatile: retransmit only to the supplier for
            # cancellation, never return/store them in snapshots or logs.
            guest_groups = []
            for item in accommodations:
                node = next(
                    a
                    for a in response.findall("./AccommodationList/Accommodation")
                    if a.get("Id") == item["id"]
                )
                guest_groups.append(
                    [
                        {k: p.get(k, "") for k in ("LastName", "FirstName")}
                        for p in node.findall("./Persons/Person")
                    ]
                )
            root = self._order_xml(
                snapshot,
                guest_groups,
                booking["reference_number"],
                order_id=booking["order_id"],
                cancel=True,
                accommodations=accommodations,
            )
            booking["cancel_state"] = "sending"
            booking["cancellation_quote"] = {
                "currency": snapshot["price"]["currency"],
                "items": [
                    {k: i[k] for k in ("id", "penalty", "possible_penalty", "deadline_date")}
                    for i in booking["items"]
                ],
            }
            self._save(service, snapshot)
            try:
                request(ctx, "order_cancel", "OrderRequest", root, config, mutation=True)
                booking["cancel_state"] = "requested"
                self._save(service, snapshot)
                return self._retrieve_locked(ctx, service, snapshot, config)
            except AdapterError as exc:
                booking["cancel_state"] = "unknown" if exc.category == "booking_unknown" else "failed"
                self._save(service, snapshot)
                raise

    def issue(self, ctx, issue_request):
        raise AdapterError(
            "NOT_SUPPORTED", "A&A: отдельная выписка билетов не предоставляется", category="validation"
        )

    def refund_quote(self, ctx, request):
        raise AdapterError(
            "NOT_SUPPORTED",
            "A&A: отдельный refund API не документирован; штраф доступен в OrderInfo",
            category="validation",
        )
