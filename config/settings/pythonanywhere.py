from .prod import *  # noqa: F401,F403
from .base import BASE_DIR, env

# PythonAnywhere free accounts do not provide PostgreSQL. Use a persistent
# SQLite database by default while still allowing DATABASE_URL to override it.
DATABASES = {
    "default": {
        **env.db_url(
            "DATABASE_URL",
            default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        ),
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 0,
    }
}

if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"]["OPTIONS"] = {
        "timeout": env.int("SQLITE_TIMEOUT", default=20),
    }
    # Free PythonAnywhere has no always-on worker process. Only jobs that are
    # fully local, short-lived and do not call external providers run inline.
    SYNC_JOB_KINDS = (
        "services.search",
        "orders.cancel",
    )
else:
    SYNC_JOB_KINDS = ()

# PythonAnywhere terminates HTTPS at its proxy. This can be enabled later once
# the web app and proxy headers have been verified.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
