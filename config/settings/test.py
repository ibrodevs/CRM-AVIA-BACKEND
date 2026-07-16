"""Тестовые настройки: PostgreSQL (обязателен для SKIP LOCKED/constraints), быстрый hasher."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

DATABASES = {
    "default": {
        **env.db_url("TEST_DATABASE_URL", default="postgres:///travelhub_test"),
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # только для скорости тестов

FIELD_ENCRYPTION_KEY = "3jJ0deI6M0y5d3G7Zbb8xxL4wXO2ldeYmyIQXjmdKF0="  # test-only

# Rate limit в тестах не мешает многократным логинам (brute force тестируется
# отдельным DB-механизмом FailedLoginAttempt, а не DRF throttle).
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {  # noqa: F405
    "login": "10000/min",
    "password_reset": "10000/min",
    "search": "10000/min",
    "public_response": "10000/min",
    "export": "10000/min",
}}

LOGGING = {"version": 1, "disable_existing_loggers": True}
