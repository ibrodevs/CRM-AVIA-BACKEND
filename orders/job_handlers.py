"""Фоновые задания заказов."""
from django.db import transaction

from common.jobs import job_handler
from common.models import BackgroundJob


@job_handler("orders.cancel", user_cancellable=False)
def cancel_order_job(job: BackgroundJob) -> dict:
    """Отмена заказа: аннулирует активные услуги (через адаптеры — Этап 4),
    затем переводит заказ в cancelled."""
    from accounts.models import User
    from orders.models import Order
    from orders.services import transition_order
    from services.models import OrderService

    payload = job.payload
    user = User.objects.get(pk=payload["user_id"])
    cancelled_services: list[str] = []

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=payload["order_id"])
        active = order.services.filter(status__in=["booked", "confirmed", "issued"])
        for service in active:
            # Этап 4: здесь вызывается provider adapter аннуляции.
            service.status = OrderService.Status.CANCELLED
            service.updated_by = user
            service.version += 1
            service.save(update_fields=["status", "updated_by", "version", "updated_at"])
            cancelled_services.append(str(service.id))

    transition_order(
        order_id=payload["order_id"], user=user, target_status=Order.Status.CANCELLED,
        reason=payload.get("reason", ""),
        expected_version=payload.get("expected_version"),
    )
    return {"cancelled_services": cancelled_services}
