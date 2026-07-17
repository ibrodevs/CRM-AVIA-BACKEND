from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]


if not FIELD_ENCRYPTION_KEY:  # noqa: F405
    FIELD_ENCRYPTION_KEY = "3jJ0deI6M0y5d3G7Zbb8xxL4wXO2ldeYmyIQXjmdKF0="
