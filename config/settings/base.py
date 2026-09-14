from datetime import timedelta
from pathlib import Path

import environ
from corsheaders.defaults import default_headers, default_methods

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-only-key-change-me-0123456789abcdef")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])


DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
]

LOCAL_APPS = [
    "common",
    "tenancy",
    "accounts",
    "crm",
    "travel_policy",
    "orders",
    "services",
    "avia",
    "rail",
    "hotels",
    "groups_app",
    "offers",
    "suppliers",
    "booking",
    "finance",
    "documents",
    "aftersales",
    "communications",
    "notifications",
    "calendar_app",
    "workforce",
    "integrations",
    "reports",
    "search",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "common.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tenancy.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Public API clients authenticate explicitly with Bearer JWT, not browser cookies.
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_URLS_REGEX = r"^/api/.*$"
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_METHODS = (*default_methods, "HEAD")
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key", "x-request-id", "if-none-match", "x-api-token", "x-api-key")
CORS_EXPOSE_HEADERS = ("X-Request-ID", "Content-Disposition", "ETag", "Retry-After")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


DATABASES = {
    "default": {
        **env.db_url("DATABASE_URL", default="postgres:///travelhub"),
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


AUTH_USER_MODEL = "accounts.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "ru"
TIME_ZONE = env("BUSINESS_TIMEZONE", default="Asia/Bishkek")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "media"


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.api_keys.IntegrationKeyAuthentication",
        "accounts.authentication.SessionAwareJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "common.errors.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "common.schema.SafeAutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "password_reset": "5/min",
        "search": "60/min",
        "public_response": "30/min",
        "export": "10/min",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "URL_FORMAT_OVERRIDE": None,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default=SECRET_KEY),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Travel Hub CRM API",
    "DESCRIPTION": "REST API backend Travel Hub CRM. Все бизнес-операции выполняются "
    "через документированные команды с проверкой прав, идемпотентностью и аудитом.",
    "VERSION": "1.0.0",
    "OAS_VERSION": "3.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
}


BUSINESS_TIMEZONE = TIME_ZONE
BASE_CURRENCY = env("BASE_CURRENCY", default="USD")
EVENT_RETENTION_DAYS = env.int("EVENT_RETENTION_DAYS", default=7)
IDEMPOTENCY_RETENTION_DAYS = env.int("IDEMPOTENCY_RETENTION_DAYS", default=30)
MULTI_CITY_MAX_SEGMENTS = env.int("MULTI_CITY_MAX_SEGMENTS", default=6)
ALLOW_MOCK_ADAPTER = env.bool("ALLOW_MOCK_ADAPTER", default=False)


FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")


# ——— Исходящие каналы доставки ———————————————————————————————————————————
# Каждый канал включается наличием своих реквизитов. Пустое значение означает
# «канал не настроен»: доставка помечается skipped, а интерфейс показывает это
# оператору вместо слова «отправлено» (см. common/transports.py).
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="")

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_API_URL = env("TELEGRAM_API_URL", default="https://api.telegram.org")

WHATSAPP_API_URL = env("WHATSAPP_API_URL", default="")
WHATSAPP_API_TOKEN = env("WHATSAPP_API_TOKEN", default="")

MAX_API_URL = env("MAX_API_URL", default="")
MAX_API_TOKEN = env("MAX_API_TOKEN", default="")

SMS_GATEWAY_URL = env("SMS_GATEWAY_URL", default="")
SMS_GATEWAY_TOKEN = env("SMS_GATEWAY_TOKEN", default="")
SMS_GATEWAY_SENDER = env("SMS_GATEWAY_SENDER", default="")

# Подписи входящих webhook мессенджеров: "telegram=secret,whatsapp=secret".
WEBHOOK_SECRETS = env.dict("WEBHOOK_SECRETS", default={})

DELIVERY_MAX_ATTEMPTS = env.int("DELIVERY_MAX_ATTEMPTS", default=5)
DELIVERY_BATCH_SIZE = env.int("DELIVERY_BATCH_SIZE", default=100)


JOB_RUNNER = {
    "BATCH_SIZE": env.int("JOB_BATCH_SIZE", default=10),
    "HEARTBEAT_SECONDS": env.int("JOB_HEARTBEAT_SECONDS", default=30),
    "STALE_AFTER_SECONDS": env.int("JOB_STALE_AFTER_SECONDS", default=180),
    "DEFAULT_MAX_ATTEMPTS": env.int("JOB_DEFAULT_MAX_ATTEMPTS", default=5),
    "POLL_INTERVAL_SECONDS": env.float("JOB_POLL_INTERVAL_SECONDS", default=1.0),
}


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "common.logging.JSONFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"level": "WARNING"},
        "travelhub": {"level": "INFO"},
    },
}
