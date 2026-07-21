from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from orders.selectors import get_order_or_404
from orders.serializers import ParticipantSerializer


class OrderParticipantDetailView(APIView):
    """Update or remove a participant that belongs to an order."""

    permission_classes = [require("orders.change")]

    def _participant(self, request, order_id, participant_id):
        order = get_order_or_404(request.user, order_id)
        participant = order.participants.filter(pk=participant_id, status="active").first()
        if participant is None:
            raise ApiError(code="NOT_FOUND", message="Участник не найден", status_code=404)
        return order, participant

    def patch(self, request, order_id, participant_id):
        order, participant = self._participant(request, order_id, participant_id)
        serializer = ParticipantSerializer(participant, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save(updated_by=request.user)
        emit_event("order.updated", order, payload={"action": "participant_updated"})
        audit("order.participant_updated", actor=request.user, resource=order, request=request)
        return Response(ParticipantSerializer(updated).data)

    def delete(self, request, order_id, participant_id):
        order, participant = self._participant(request, order_id, participant_id)
        if participant.service_passengers.exclude(status__in=["cancelled", "replaced"]).exists():
            raise ApiError(
                code="PARTICIPANT_HAS_SERVICES",
                message="Участник привязан к услугам; сначала cancel/replace",
                status_code=409,
            )
        participant.status = "removed"
        participant.updated_by = request.user
        participant.save(update_fields=["status", "updated_by", "updated_at"])
        emit_event("order.updated", order, payload={"action": "participant_removed"})
        audit("order.participant_removed", actor=request.user, resource=order, request=request)
        return Response(status=http.HTTP_204_NO_CONTENT)
