from rest_framework import serializers
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from orders.selectors import get_order_or_404
from orders.serializers import ParticipantSerializer


class OrderAwareParticipantSerializer(ParticipantSerializer):
    """Participant serializer with tenant checks available before model save."""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        order = self.context["order"]
        person = attrs.get("person", getattr(self.instance, "person", None))
        booking_document = attrs.get(
            "booking_document", getattr(self.instance, "booking_document", None)
        )
        if person is not None and person.tenant_id != order.tenant_id:
            raise serializers.ValidationError({"person": ["Человек относится к другому tenant"]})
        if booking_document is not None and booking_document.tenant_id != order.tenant_id:
            raise serializers.ValidationError(
                {"booking_document": ["Документ относится к другому tenant"]}
            )
        if booking_document is not None and person is not None and booking_document.person_id != person.id:
            raise serializers.ValidationError(
                {"booking_document": ["Документ не принадлежит выбранному человеку"]}
            )
        return attrs


class OrderParticipantsView(APIView):
    """List or add participants with order-aware tenant validation."""

    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        participants = order.participants.filter(status="active").select_related("person")
        return Response(ParticipantSerializer(participants, many=True).data)

    def post(self, request, order_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        order = get_order_or_404(request.user, order_id)
        serializer = OrderAwareParticipantSerializer(
            data=request.data, context={"order": order, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        participant = serializer.save(tenant_id=order.tenant_id, order=order, created_by=request.user)
        emit_event("order.updated", order, payload={"action": "participant_added"})
        audit("order.participant_added", actor=request.user, resource=order, request=request)
        return Response(ParticipantSerializer(participant).data, status=http.HTTP_201_CREATED)


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
        serializer = OrderAwareParticipantSerializer(
            participant,
            data=request.data,
            partial=True,
            context={"order": order, "request": request},
        )
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
