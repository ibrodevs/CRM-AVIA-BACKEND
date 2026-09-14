"""Фиксация нарушений SLA.

Поле `breached_at` было объявлено, но никогда не записывалось, а событий `sla.*`
система не публиковала ни разу — из-за этого правило уведомлений «Просрочки и
дедлайны SLA» включалось, но не могло сработать. Задача закрывает обе дыры:
помечает нарушение и публикует `sla.breached`, после чего правило работает само.
"""

from __future__ import annotations

from django.utils import timezone

from common.outbox import emit_event
from common.scheduled import scheduled_task
from workforce.models import SlaInstance


@scheduled_task("workforce.check_sla_breaches")
def check_sla_breaches() -> str:
    now = timezone.now()
    overdue = SlaInstance.all_objects.filter(
        breached_at__isnull=True,
        resolved_at__isnull=True,
        response_deadline__lt=now,
        responded_at__isnull=True,
    ).select_related("policy", "assignee")[:500]

    marked = 0
    for instance in overdue:
        instance.breached_at = now
        instance.save(update_fields=["breached_at", "updated_at"])
        overdue_minutes = int((now - instance.response_deadline).total_seconds() // 60)
        emit_event(
            "sla.breached",
            instance,
            payload={
                "resource_type": instance.resource_type,
                "resource_id": instance.resource_id,
                "response_minutes": instance.policy.response_minutes,
                "overdue_minutes": overdue_minutes,
                "assignee": str(instance.assignee_id) if instance.assignee_id else None,
                "deadline": instance.response_deadline.isoformat(),
            },
            audience_user=instance.assignee,
            tenant_id=instance.tenant_id,
        )
        marked += 1

    # Норматив решения считается нарушенным отдельно: реакция могла быть, а
    # обращение так и осталось незакрытым.
    unresolved = SlaInstance.all_objects.filter(
        breached_at__isnull=True,
        resolved_at__isnull=True,
        resolution_deadline__lt=now,
    ).select_related("policy", "assignee")[:500]
    for instance in unresolved:
        instance.breached_at = now
        instance.save(update_fields=["breached_at", "updated_at"])
        emit_event(
            "sla.breached",
            instance,
            payload={
                "resource_type": instance.resource_type,
                "resource_id": instance.resource_id,
                "kind": "resolution",
                "overdue_minutes": int((now - instance.resolution_deadline).total_seconds() // 60),
                "assignee": str(instance.assignee_id) if instance.assignee_id else None,
                "deadline": instance.resolution_deadline.isoformat(),
            },
            audience_user=instance.assignee,
            tenant_id=instance.tenant_id,
        )
        marked += 1
    return f"marked {marked} SLA breaches"
