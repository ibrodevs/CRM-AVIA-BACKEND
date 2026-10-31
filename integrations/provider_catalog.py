"""Public integration contracts; contains no account identifiers or secrets."""

from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from integrations.adapters import get_adapter


def field(key, label, *, secret=False, required=True, kind="text", default=None):
    return {
        "key": key,
        "label": label,
        "secret": secret,
        "required": required,
        "kind": kind,
        **({"default": default} if default is not None else {}),
    }


PROVIDERS = [
    {
        "key": "hotelbook",
        "name": "Hotelbook / HBPRO",
        "kinds": ["hotel"],
        "stage": "sandbox_verified",
        "notes": "Живой тестовый цикл пройден. Production требует допуска HBPRO.",
        "operations": ["search", "revalidate", "book", "retrieve", "cancel"],
        "fields": [field("login", "Логин"), field("password", "Пароль", secret=True)],
    },
    {
        "key": "aanda",
        "name": "A&A / Roomlink",
        "kinds": ["hotel"],
        "stage": "awaiting_live_confirmation",
        "notes": "Код полного цикла готов. Последняя живая приёмка остановилась на pending / AllowCancel=Нет.",
        "operations": ["search", "revalidate", "book", "retrieve", "cancel"],
        "fields": [
            field("buyer_id", "BuyerId"),
            field("user_id", "UserId"),
            field("password", "Пароль", secret=True),
        ],
    },
    {
        "key": "ttbooking",
        "name": "TTBooking",
        "kinds": ["avia"],
        "stage": "awaiting_sandbox_acceptance",
        "notes": "Контракт подтверждён поддержкой: синхронные split-команды, PAYDEPOSIT, реальные билеты и PNR. Требуется живая sandbox-приёмка; возврат и обмен через кабинет/менеджера.",
        "operations": ["search", "revalidate", "fare_rules", "book", "retrieve", "issue", "cancel"],
        "fields": [
            field("url", "URL JSON API", default="https://dev.ttbooking.ru/api/scbs/json"),
            field("agency", "Agency ID"),
            field("user", "User ID"),
            field("password_key", "Password Key", secret=True),
            field("protocol", "Протокол", default="async"),
            field("currency", "Валюта договора", default="RUB"),
        ],
    },
    {
        "key": "ostrovok",
        "name": "Островок / ETG / RateHawk",
        "kinds": ["hotel"],
        "stage": "awaiting_credentials_acceptance",
        "notes": "Реализованы документированные методы ETG v3. Нужны API key ID/key, права договора и полная sandbox-приёмка. Карточные платежи не поддерживаются.",
        "operations": ["search", "revalidate", "book", "retrieve", "cancel"],
        "fields": [
            field("url", "URL API", default="https://api.worldota.net"),
            field("key_id", "API key ID"),
            field("api_key", "API key", secret=True),
            field("residency", "Резидентство гостей (код страны)", default="ru"),
            field("currency", "Валюта", default="RUB"),
            field("payment_type", "Тип оплаты договора", default="deposit"),
        ],
    },
    {
        "key": "iway",
        "name": "i’way v4",
        "kinds": ["transfer"],
        "stage": "awaiting_partner_acceptance",
        "notes": "API v4: общий sandbox из руководства, кресла и допуслуги, создание и отдельное approve. Финансовые условия — у клиентского менеджера; требуется приёмка по разделу 8.",
        "operations": ["search", "revalidate", "book", "retrieve", "issue", "cancel"],
        "fields": [
            field("url", "API_URL (без /v4)", default="https://sandbox.iway.ru/transnextgen"),
            field("login", "Логин"),
            field("password", "Пароль", secret=True),
            field("user_id", "User ID"),
        ],
    },
    {
        "key": "onelya",
        "name": "Инновационная мобильность / Onelya",
        "kinds": ["rail"],
        "stage": "documentation_blocked",
        "notes": "Вход в актуальную ApiDocs запрещён. Подготовлены авторизация и V1-поиск; бронь, выписка и отмена отключены до получения актуального контракта. Минимальная цена поиска не является финальным тарифом.",
        "operations": ["search"],
        "fields": [
            field("url", "URL API", default="https://api-test.onelya.ru"),
            field("login", "Логин"),
            field("password", "Пароль", secret=True),
            field("pos", "POS (выдаёт поставщик)"),
            field("currency", "Валюта договора", default="RUB"),
        ],
    },
]


class ProviderCatalogView(APIView):
    permission_classes = [require("suppliers.view", "suppliers.change", "suppliers.manage_credentials")]

    def get(self, request):
        return Response(
            {
                "results": [
                    {**provider, "environment": get_adapter(provider["key"]).configured_environment()}
                    for provider in PROVIDERS
                ]
            }
        )
