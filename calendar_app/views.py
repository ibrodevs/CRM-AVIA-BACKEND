from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from calendar_app.models import CalendarEvent, Trip, TripConflict
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from common.pagination import DefaultPagination


class TripSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)

    class Meta:
        model = Trip
        fields = [
            "id",
            "order",
            "order_number",
            "title",
            "starts_at",
            "ends_at",
            "status",
            "criticality",
            "computed_at",
        ]


class TripConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = TripConflict
        fields = ["id", "kind", "severity", "details", "detected_at", "resolved_at"]


class CalendarEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarEvent
        fields = [
            "id",
            "kind",
            "title",
            "description",
            "starts_at",
            "ends_at",
            "timezone",
            "order",
            "service",
            "person",
            "supplier",
            "assignee",
            "scope",
            "priority",
            "notification_method",
            "recurrence_rule",
            "criterion",
            "action_on_problem",
            "status",
            "completed_at",
            "version",
        ]
        read_only_fields = ["id", "status", "completed_at", "version"]

    def validate(self, attrs):
        request = self.context.get("request")
        if request is None:
            return attrs
        tenant_id = request.user.tenant_id
        order = attrs.get("order") or getattr(self.instance, "order", None)
        service = attrs.get("service") or getattr(self.instance, "service", None)
        person = attrs.get("person") or getattr(self.instance, "person", None)
        supplier = attrs.get("supplier") or getattr(self.instance, "supplier", None)
        assignee = attrs.get("assignee") or getattr(self.instance, "assignee", None)
        fields = {}
        for field, obj in (
            ("order", order),
            ("service", service),
            ("person", person),
            ("supplier", supplier),
            ("assignee", assignee),
        ):
            if obj and obj.tenant_id != tenant_id:
                fields[field] = ["Объект не найден в текущей организации"]
        if service and order and service.order_id != order.id:
            fields["service"] = ["Услуга должна принадлежать выбранному заказу"]
        if fields:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Некорректные связи события календаря",
                fields=fields,
                status_code=400,
            )
        return attrs


class TripListView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request):
        qs = Trip.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True).select_related(
            "order"
        )
        params = request.query_params
        if from_date := params.get("from"):
            qs = qs.filter(starts_at__gte=from_date)
        if to_date := params.get("to"):
            qs = qs.filter(starts_at__lte=to_date)
        if trip_status := params.get("status"):
            qs = qs.filter(status=trip_status)
        page = self.paginate_queryset(qs.order_by("starts_at"))
        return self.get_paginated_response(TripSerializer(page, many=True).data)


class TripConflictsView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, trip_id):
        trip = Trip.objects.filter(pk=trip_id, tenant_id=request.user.tenant_id).first()
        if trip is None:
            raise ApiError(code="NOT_FOUND", message="Поездка не найдена", status_code=404)
        conflicts = trip.conflicts.filter(resolved_at__isnull=True)
        return Response(TripConflictSerializer(conflicts, many=True).data)


def _events_qs(request):
    return CalendarEvent.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True).filter(
        Q(scope__in=["team", "tenant"]) | Q(assignee=request.user) | Q(created_by=request.user)
    )


class CalendarEventListCreateView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = CalendarEventSerializer

    def get(self, request):
        qs = _events_qs(request)
        params = request.query_params
        if from_date := params.get("from"):
            qs = qs.filter(starts_at__gte=from_date)
        if to_date := params.get("to"):
            qs = qs.filter(starts_at__lte=to_date)
        if kind := params.get("kind"):
            qs = qs.filter(kind=kind)
        if event_status := params.get("status"):
            qs = qs.filter(status=event_status)
        page = self.paginate_queryset(qs.order_by("starts_at"))
        return self.get_paginated_response(CalendarEventSerializer(page, many=True).data)

    def post(self, request):
        serializer = CalendarEventSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        event = serializer.save(
            tenant_id=request.user.tenant_id,
            created_by=request.user,
            assignee=serializer.validated_data.get("assignee") or request.user,
        )
        emit_event(
            "calendar.event.updated", event, payload={"action": "created"}, audience_user=event.assignee
        )
        audit("calendar.event_created", actor=request.user, resource=event, request=request)
        return Response(CalendarEventSerializer(event).data, status=http.HTTP_201_CREATED)


class CalendarEventCheckDuplicateView(APIView):
    permission_classes = [require("orders.view")]

    def post(self, request):
        starts_at = request.data.get("starts_at")
        title = str(request.data.get("title", "")).strip()
        if not starts_at or not title:
            raise ApiError(code="VALIDATION_ERROR", message="Нужны title и starts_at", status_code=400)
        candidates = _events_qs(request).filter(title__iexact=title, starts_at=starts_at, status="scheduled")
        return Response({"duplicates": CalendarEventSerializer(candidates, many=True).data})


class CalendarEventCompleteView(APIView):
    permission_classes = [require("orders.view")]

    def post(self, request, event_id):
        event = _events_qs(request).filter(pk=event_id).first()
        if event is None:
            raise ApiError(code="NOT_FOUND", message="Событие не найдено", status_code=404)
        if event.status != CalendarEvent.Status.SCHEDULED:
            raise ApiError(
                code="INVALID_EVENT_STATUS", message="Событие уже завершено/отменено", status_code=409
            )
        occurrence_at = request.data.get("occurrence_at")
        if event.recurrence_rule and occurrence_at:
            from calendar_app.models import CalendarEventOccurrence

            occurrence, _ = CalendarEventOccurrence.objects.get_or_create(
                event=event, occurs_at=occurrence_at
            )
            occurrence.status = "done"
            occurrence.completed_at = timezone.now()
            occurrence.completed_by = request.user
            occurrence.save()
        else:
            event.status = CalendarEvent.Status.DONE
            event.completed_at = timezone.now()
            event.completed_by = request.user
            event.save(update_fields=["status", "completed_at", "completed_by"])
        emit_event("calendar.event.updated", event, payload={"action": "completed"})
        audit("calendar.event_completed", actor=request.user, resource=event, request=request)
        return Response(CalendarEventSerializer(event).data)


class CalendarEventRescheduleView(APIView):
    permission_classes = [require("orders.view")]

    def post(self, request, event_id):
        event = _events_qs(request).filter(pk=event_id).first()
        if event is None:
            raise ApiError(code="NOT_FOUND", message="Событие не найдено", status_code=404)
        new_starts = request.data.get("starts_at")
        if not new_starts:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="starts_at обязателен",
                fields={"starts_at": ["Обязательное поле"]},
                status_code=400,
            )
        old = event.starts_at
        serializer = CalendarEventSerializer(
            event,
            data={"starts_at": new_starts, "ends_at": request.data.get("ends_at")},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(version=event.version + 1, updated_by=request.user)
        emit_event("calendar.event.updated", event, payload={"action": "rescheduled"})
        audit(
            "calendar.event_rescheduled",
            actor=request.user,
            resource=event,
            request=request,
            before={"starts_at": str(old)},
            after={"starts_at": new_starts},
        )
        return Response(CalendarEventSerializer(event).data)


class CalendarFeedView(APIView):
    """GET /calendar/feed/?from=&to=&view= — сводный feed поездок и событий."""

    permission_classes = [require("orders.view")]

    def get(self, request):
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")
        events = _events_qs(request).filter(status="scheduled")
        trips = Trip.objects.filter(
            tenant_id=request.user.tenant_id, archived_at__isnull=True
        ).select_related("order")
        if from_date:
            events = events.filter(starts_at__gte=from_date)
            trips = trips.filter(starts_at__gte=from_date)
        if to_date:
            events = events.filter(starts_at__lte=to_date)
            trips = trips.filter(starts_at__lte=to_date)
        return Response(
            {
                "events": CalendarEventSerializer(events.order_by("starts_at")[:500], many=True).data,
                "trips": TripSerializer(trips.order_by("starts_at")[:500], many=True).data,
            }
        )
