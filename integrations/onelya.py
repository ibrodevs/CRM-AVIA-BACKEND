"""Onelya V1 read-only foundation.

Reservation/Confirm/Cancel are deliberately blocked until the current supplier
contract is accessible. The supplied ApiDocs login currently denies access.
Read-only routes follow the published onelya_sdk 1.5.1 client; no guessed sale
payloads are sent using an old third-party SDK contract.
"""

import base64
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from integrations.adapters import AdapterContext, AdapterError
from integrations.json_provider import JsonProviderAdapter


class OnelyaAdapter(JsonProviderAdapter):
    key = "onelya"
    supported_kinds = ["rail"]
    environment_setting = "ONELYA_ENVIRONMENT"
    default_url = "https://api-test.onelya.ru"
    required_config = ("login", "password", "pos", "currency")

    def _call(self, ctx, config, operation, data):
        auth = base64.b64encode(f"{config['login']}:{config['password']}".encode()).decode()
        result = self._http(
            ctx,
            config,
            operation.replace("/", "_"),
            "POST",
            "/" + operation,
            data,
            headers={"Authorization": "Basic " + auth, "Pos": str(config["pos"])},
        )
        if not isinstance(result, dict) or "Code" in result:
            raise AdapterError(
                "ONELYA_REJECTED", "Onelya отклонил запрос; проверьте доступ и POS", category="validation"
            )
        return result

    def verify_credentials(self, credential):
        ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=credential.supplier_id)
        _, config = self._config(ctx, credential)
        self._call(ctx, config, "Partner/V1/Info/Balances", {})
        return "Onelya: доступ к справочной операции подтверждён; продажа отключена до получения актуального контракта"

    def search(self, ctx, kind, criteria):
        if kind != "rail":
            raise AdapterError("NOT_SUPPORTED", "Onelya: только ЖД", category="validation")
        _, config = self._config(ctx)
        origin, destination = str(criteria.get("origin") or ""), str(criteria.get("destination") or "")
        if not origin.isdigit() or not destination.isdigit():
            raise AdapterError(
                "STATION_CODE_REQUIRED",
                "Укажите коды станций Onelya/Экспресс, а не названия городов",
                category="validation",
            )
        try:
            departure = date.fromisoformat(str(criteria.get("date") or ""))
        except ValueError:
            raise AdapterError(
                "SEARCH_CRITERIA_INVALID", "Укажите дату отправления", category="validation"
            ) from None
        data = self._call(
            ctx,
            config,
            "Railway/V1/Search/TrainPricing",
            {
                "Origin": origin,
                "Destination": destination,
                "DepartureDate": departure.isoformat() + "T00:00:00",
                "TimeFrom": 0,
                "TimeTo": 24,
                "CarGrouping": "Group",
                "SpecialPlacesDemand": "NoValue",
            },
        )
        offers = []
        for train in data.get("Trains") or []:
            for group in train.get("CarGroups") or []:
                if (
                    group.get("IsSaleForbidden")
                    or not group.get("PlaceQuantity")
                    or group.get("MinPrice") is None
                ):
                    continue
                amount = Decimal(str(group["MinPrice"]).replace(" ", "").replace(",", "."))
                if not amount.is_finite() or amount < 0:
                    continue
                offers.append(
                    {
                        "provider_adapter": self.key,
                        "provider_context": {
                            "supplier_id": str(ctx.supplier_id),
                            "environment": self.configured_environment(),
                        },
                        "kind": "rail",
                        "external_key": f"{train['TrainNumber']}:{group.get('CarType')}:{group.get('ServiceClass')}",
                        "price": {"amount": str(amount), "currency": config["currency"]},
                        "availability": "available",
                        "expires_at": (timezone.now() + timedelta(minutes=5)).isoformat(),
                        "itinerary": {
                            "train_number": train["TrainNumber"],
                            "origin": train.get("OriginName", origin),
                            "destination": train.get("DestinationName", destination),
                            "departure": train.get("DepartureDateTime"),
                            "arrival": train.get("ArrivalDateTime"),
                            "car_type": group.get("CarType"),
                            "service_class": group.get("ServiceClass"),
                            "places": group.get("PlaceQuantity"),
                        },
                        "fare": {
                            "estimated": True,
                            "booking_available": False,
                            "notice": "Предварительная минимальная цена. Продажа отключена до получения актуального контракта Onelya.",
                        },
                        "onelya": {
                            "criteria": deepcopy(criteria),
                            "train_number": train["TrainNumber"],
                            "car_type": group.get("CarType"),
                            "service_class": group.get("ServiceClass"),
                        },
                    }
                )
        return offers

    def revalidate(self, ctx, snapshot):
        raise AdapterError(
            "SUPPLIER_DOCUMENTATION_REQUIRED",
            "Нужна актуальная документация CarPricing/Reservation и выбор конкретных мест. Минимальная цена поиска не является подтверждённым тарифом.",
            category="configuration",
        )

    def book(self, ctx, request):
        raise AdapterError(
            "SUPPLIER_DOCUMENTATION_REQUIRED",
            "Onelya: продажа не включена без актуального контракта и приёмки поставщика",
            category="configuration",
        )

    def retrieve_booking(self, ctx, locator):
        raise AdapterError(
            "SUPPLIER_DOCUMENTATION_REQUIRED",
            "Onelya: требуется актуальный контракт получения заказа",
            category="configuration",
        )

    def cancel(self, ctx, locator):
        raise AdapterError(
            "SUPPLIER_DOCUMENTATION_REQUIRED",
            "Onelya: отмена не включена без актуального контракта",
            category="configuration",
        )
