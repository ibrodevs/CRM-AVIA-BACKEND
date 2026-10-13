from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"

    def ready(self):
        # Register production adapters after Django app loading to avoid import cycles.
        from integrations.adapters import register_adapter
        from integrations.ttbooking import TTBookingAdapter

        register_adapter(TTBookingAdapter())
