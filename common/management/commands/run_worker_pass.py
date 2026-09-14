"""Один проход всей фоновой работы — для хостингов без постоянного воркера.

`run_jobs` рассчитан на отдельный процесс, который живёт всё время, а
`run_scheduled_jobs` — на минутный cron. На PythonAnywhere нет ни того, ни
другого: консоль закрывается, а планировщик даёт ограниченное число запусков.
Эта команда выполняет обе части за один вызов и завершается, поэтому её можно
повесить на scheduled task, на always-on task в цикле или дёрнуть по HTTP.

    python manage.py run_worker_pass                # один проход
    python manage.py run_worker_pass --loop --seconds 3300
"""

import logging
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger("travelhub.jobs")


class Command(BaseCommand):
    help = "Выполняет один проход outbox + фоновых заданий + периодических задач"

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Повторять проходы до истечения --seconds (для always-on task)",
        )
        parser.add_argument(
            "--seconds",
            type=int,
            default=3300,
            help="Сколько секунд работать в режиме --loop (по умолчанию 55 минут)",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Пауза между проходами в режиме --loop, секунд",
        )
        parser.add_argument(
            "--skip-scheduled",
            action="store_true",
            help="Только outbox и задания, без периодических задач",
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            self._pass(options)
            return

        deadline = time.monotonic() + options["seconds"]
        passes = 0
        while time.monotonic() < deadline:
            self._pass(options)
            passes += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(options["interval"], remaining))
        self.stdout.write(f"Проходов выполнено: {passes}")

    def _pass(self, options) -> None:
        # run_jobs --once забирает готовые задания и обрабатывает outbox-события;
        # именно из outbox рождаются уведомления и записи очередей доставки.
        try:
            call_command("run_jobs", "--once")
        except Exception:
            logger.exception("worker pass: run_jobs failed")
            self.stderr.write("run_jobs: FAILED")

        if options["skip_scheduled"]:
            return

        # А периодические задачи разбирают сами очереди доставки и дедлайны.
        try:
            call_command("run_scheduled_jobs")
        except Exception:
            logger.exception("worker pass: run_scheduled_jobs failed")
            self.stderr.write("run_scheduled_jobs: FAILED")
