from django.db import transaction
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.locking import check_version
from common.outbox import emit_event
from common.pagination import DefaultPagination
from orders.selectors import get_order_or_404
from orders.serializers import OrderTaskSerializer, RoutePointSerializer, RouteSerializer


class OrderRouteDetailView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        route = getattr(order, "route", None)
        if route is None:
            raise ApiError(code="NOT_FOUND", message="Маршрут не задан", status_code=404)
        return Response(RouteSerializer(route).data)

    def patch(self, request, order_id):
        from orders.models import Route, RoutePoint

        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)

        order = get_order_or_404(request.user, order_id)
        with transaction.atomic():
            route = Route.objects.select_for_update().filter(order=order).first()
            if route is None:
                route = Route.objects.create(tenant_id=order.tenant_id, order=order, created_by=request.user)
            else:
                check_version(route, request.data.get("version"))

            kind = request.data.get("kind")
            if kind is not None:
                valid_kinds = {choice for choice, _ in Route.Kind.choices}
                if kind not in valid_kinds:
                    raise ApiError(
                        code="VALIDATION_ERROR",
                        message="Некорректный тип маршрута",
                        fields={"kind": ["Недопустимое значение"]},
                        status_code=400,
                    )
                route.kind = kind

            points = request.data.get("points")
            if points is not None:
                if not isinstance(points, list) or len(points) < 2:
                    raise ApiError(
                        code="VALIDATION_ERROR",
                        message="Маршрут содержит минимум 2 точки",
                        fields={"points": ["Минимум 2 точки"]},
                        status_code=400,
                    )
                if route.kind == Route.Kind.MULTI_CITY and len(points) > 7:
                    raise ApiError(
                        code="VALIDATION_ERROR",
                        message="Превышено максимальное количество точек",
                        fields={"points": ["Максимум 7 точек"]},
                        status_code=400,
                    )
                validated_points = []
                for point in points:
                    point_serializer = RoutePointSerializer(data=point)
                    point_serializer.is_valid(raise_exception=True)
                    validated_points.append(point_serializer.validated_data)

                route.points.all().delete()
                for index, point_data in enumerate(validated_points, start=1):
                    RoutePoint.objects.create(
                        tenant_id=order.tenant_id,
                        route=route,
                        sequence=index,
                        created_by=request.user,
                        **point_data,
                    )

            route.version += 1
            route.updated_by = request.user
            route.save()
            emit_event("order.updated", order, payload={"action": "route_changed"})
            audit("order.route_changed", actor=request.user, resource=order, request=request)

        return Response(RouteSerializer(route).data)

    def put(self, request, order_id):
        return self.patch(request, order_id)


class OrderTaskCollectionView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        qs = order.tasks.order_by("due_at", "created_at")
        if task_status := request.query_params.get("status"):
            qs = qs.filter(status=task_status)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(OrderTaskSerializer(page, many=True).data)

    def post(self, request, order_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)

        order = get_order_or_404(request.user, order_id)
        serializer = OrderTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = serializer.save(tenant_id=order.tenant_id, order=order, created_by=request.user)
        emit_event("order.updated", order, payload={"action": "task_created", "task_id": str(task.id)}, audience_user=task.assignee)
        audit("order.task_created", actor=request.user, resource=order, request=request)
        return Response(OrderTaskSerializer(task).data, status=http.HTTP_201_CREATED)
