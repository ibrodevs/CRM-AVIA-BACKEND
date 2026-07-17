from django.apps import AppConfig


class CommonConfig(AppConfig):
    name = "common"
    verbose_name = "Инфраструктура"

    def ready(self):

        from django.utils.module_loading import autodiscover_modules

        autodiscover_modules("job_handlers")
        autodiscover_modules("scheduled_tasks")
        autodiscover_modules("outbox_handlers")
        from common import maintenance  # noqa: F401  (встроенные retention-задачи)
