"""Периодические проверки (cron / Kubernetes CronJob):

    * * * * * python manage.py run_scheduled_jobs

Команда идемпотентна и защищена PostgreSQL advisory lock от параллельного
запуска (ТЗ §3.2).
"""
import logging

from django.core.management.base import BaseCommand
from django.db import connection

from common.scheduled import all_scheduled

logger = logging.getLogger("travelhub.scheduled")

ADVISORY_LOCK_KEY = 0x7472_6156  # 'traV' — ключ advisory lock команды


class Command(BaseCommand):
    help = "Выполняет зарегистрированные периодические задачи под advisory lock"

    def add_arguments(self, parser):
        parser.add_argument("--only", default=None, help="Выполнить только задачу с этим именем")

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
            acquired = cursor.fetchone()[0]
        if not acquired:
            self.stdout.write("Другой экземпляр run_scheduled_jobs уже выполняется — выход.")
            return

        try:
            tasks = all_scheduled()
            if options["only"]:
                tasks = {k: v for k, v in tasks.items() if k == options["only"]}
            for name, func in tasks.items():
                try:
                    summary = func()
                    logger.info("scheduled task done", extra={"task": name, "summary": summary})
                    self.stdout.write(f"{name}: {summary or 'ok'}")
                except Exception:
                    logger.exception("scheduled task failed", extra={"task": name})
                    self.stderr.write(f"{name}: FAILED")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])
