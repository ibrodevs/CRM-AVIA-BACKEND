from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from orders.models import OrderTask
from orders.selectors import get_order_or_404
from orders.serializers import OrderTaskSerializer


class OrderTaskDetailView(APIView):
    permission_classes = [require("orders.view")]

    def _task(self, request, order_id, task_id):
        order = get_order_or_404(request.user, order_id)
        task = OrderTask.objects.filter(order=order, pk=task_id).first()
        if task is None:
            raise ApiError(code="NOT_FOUND", message="Задача не найдена", status_code=404)
        return order, task

    def patch(self, request, order_id, task_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)

        order, task = self._task(request, order_id, task_id)
        serializer = OrderTaskSerializer(task, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        next_status = serializer.validated_data.get("status", task.status)
        completed_at = task.completed_at
        if next_status == "completed" and task.status != "completed":
            completed_at = timezone.now()
        elif next_status != "completed":
            completed_at = None
        task = serializer.save(updated_by=request.user, completed_at=completed_at)
        emit_event("order.updated", order, payload={"action": "task_updated", "task_id": str(task.id)})
        audit("order.task_updated", actor=request.user, resource=order, request=request)
        return Response(OrderTaskSerializer(task).data)

    def delete(self, request, order_id, task_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)

        order, task = self._task(request, order_id, task_id)
        task_id_value = str(task.id)
        task.delete()
        emit_event("order.updated", order, payload={"action": "task_deleted", "task_id": task_id_value})
        audit("order.task_deleted", actor=request.user, resource=order, request=request)
        return Response(status=http.HTTP_204_NO_CONTENT)
