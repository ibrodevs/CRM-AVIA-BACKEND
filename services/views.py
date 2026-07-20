from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, has_service_action, require
from common.audit import audit
from common.errors import ApiError, TransitionForbiddenError
from common.idempotency import idempotent_command
from common.jobs import enqueue
from common.locking import check_version
from common.money import MoneyField
from common.outbox import emit_event
from common.pagination import DefaultPagination
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from orders.selectors import get_order_or_404
from services.models import (
    SERVICE_TRANSITIONS,
    OrderService,
    SearchSession,
    ServiceExtra,
    ServiceOffer,
)


class SearchSessionSerializer(serializers.ModelSerializer):
    provider_runs = serializers.SerializerMethodField()

    class Meta:
        model = SearchSession
        fields = ["id", "kind", "criteria", "status", "order", "expires_at", "created_at", "provider_runs"]

    def get_provider_runs(self, obj):
        return [
            {
                "id": str(r.id),
                "supplier": str(r.supplier_id) if r.supplier_id else None,
                "provider_adapter": r.provider_adapter,
                "status": r.status,
                "offers_count": r.offers_count,
                "error_code": r.error_code,
            }
            for r in obj.provider_runs.all()
        ]


class ServiceOfferSerializer(serializers.ModelSerializer):
    price = MoneyField(amount_field="price_amount", currency_field="price_currency")

    class Meta:
        model = ServiceOffer
        fields = [
            "id",
            "kind",
            "supplier",
            "provider_adapter",
            "external_key",
            "is_manual",
            "itinerary",
            "fare",
            "price",
            "availability",
            "expires_at",
            "applied_markup_rules",
            "compliance",
            "created_at",
        ]


class OrderServiceSerializer(serializers.ModelSerializer):
    client_price = MoneyField(amount_field="client_total", currency_field="currency")
    order_number = serializers.CharField(source="order.number", read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default="")
    passengers_count = serializers.SerializerMethodField()

    class Meta:
        model = OrderService
        fields = [
            "id",
            "order",
            "order_number",
            "kind",
            "status",
            "title",
            "supplier",
            "supplier_name",
            "passengers_count",
            "external_id",
            "source",
            "starts_at",
            "ends_at",
            "currency",
            "supplier_cost",
            "taxes",
            "agency_fee",
            "markup",
            "commission",
            "discount",
            "client_total",
            "client_price",
            "payment_deadline",
            "ticketing_deadline",
            "responsible",
            "policy_compliance",
            "cancellation_rules",
            "version",
            "created_at",
        ]
        read_only_fields = ["id", "order", "status", "version", "created_at"]

    def get_passengers_count(self, obj) -> int:
        return obj.order.participants.filter(status="active").count()


class ServiceListView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request):
        qs = OrderService.objects.filter(
            tenant_id=request.user.tenant_id, archived_at__isnull=True
        ).select_related("order", "supplier").prefetch_related("order__participants")
        if kind := request.query_params.get("kind"):
            qs = qs.filter(kind=kind)
        if service_status := request.query_params.get("status"):
            qs = qs.filter(status=service_status)
        if order_id := request.query_params.get("order"):
            qs = qs.filter(order_id=order_id)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response(OrderServiceSerializer(page, many=True).data)


class ServiceExtraSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceExtra
        fields = [
            "id",
            "catalog_item",
            "name",
            "stage",
            "availability",
            "passenger",
            "quantity",
            "price",
            "currency",
            "fee",
            "status",
            "emd_reference",
        ]
        read_only_fields = ["id", "status"]


class SearchCreateView(APIView):
    permission_classes = [require("services.search")]

    def post(self, request):
        kind = str(request.data.get("kind", ""))
        criteria = request.data.get("criteria")
        if kind not in [k for k, _ in SearchSession._meta.get_field("kind").choices]:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Неизвестный kind",
                fields={"kind": ["Недопустимое значение"]},
                status_code=400,
            )
        if not isinstance(criteria, dict):
            raise ApiError(
                code="VALIDATION_ERROR",
                message="criteria обязателен",
                fields={"criteria": ["Обязательный объект"]},
                status_code=400,
            )
        if not has_service_action(request.user, kind, "search"):
            raise ApiError(
                code="SERVICE_KIND_FORBIDDEN", message=f"Нет доступа к поиску {kind}", status_code=403
            )
        order = None
        if order_id := request.data.get("order"):
            order = get_order_or_404(request.user, order_id)
        session = SearchSession.objects.create(
            tenant_id=request.user.tenant_id,
            user=request.user,
            order=order,
            kind=kind,
            criteria=criteria,
            created_by=request.user,
        )
        job = enqueue("services.search", {"session_id": str(session.id)}, request=request)
        session.job = job
        session.save(update_fields=["job"])
        return Response({"search_id": str(session.id), "job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)


def _get_session(request, search_id) -> SearchSession:
    session = SearchSession.objects.filter(pk=search_id, tenant_id=request.user.tenant_id).first()
    if session is None:
        raise ApiError(code="NOT_FOUND", message="Поисковая сессия не найдена", status_code=404)
    return session


class SearchDetailView(APIView):
    permission_classes = [require("services.search")]

    def get(self, request, search_id):
        session = _get_session(request, search_id)
        return Response(SearchSessionSerializer(session).data)


class SearchCancelView(APIView):
    permission_classes = [require("services.search")]

    def post(self, request, search_id):
        session = _get_session(request, search_id)
        if session.status in (SearchSession.Status.PENDING, SearchSession.Status.RUNNING):
            session.status = SearchSession.Status.CANCELLED
            session.save(update_fields=["status"])
        return Response(SearchSessionSerializer(session).data)


class SearchOffersView(GenericAPIView):
    permission_classes = [require("services.search")]
    pagination_class = DefaultPagination

    def get(self, request, search_id):
        session = _get_session(request, search_id)
        qs = session.offers.all()
        params = request.query_params
        sort = params.get("sort", "best")
        if sort == "cheap":
            qs = qs.order_by("price_amount")
        elif sort == "fast" and session.kind == "avia":
            qs = qs.order_by("price_amount")
        elif sort == "early":
            qs = qs.order_by("created_at")
        else:
            qs = qs.order_by("price_amount")
        if max_price := params.get("max_price"):
            qs = qs.filter(price_amount__lte=max_price)
        if supplier := params.get("supplier"):
            qs = qs.filter(supplier_id=supplier)
        page = self.paginate_queryset(qs)
        response = self.get_paginated_response(ServiceOfferSerializer(page, many=True).data)
        response.data["session_status"] = session.status
        response.data["freshness"] = session.expires_at
        return response


def _get_offer(request, offer_id) -> ServiceOffer:
    offer = ServiceOffer.objects.filter(pk=offer_id, tenant_id=request.user.tenant_id).first()
    if offer is None:
        raise ApiError(code="NOT_FOUND", message="Предложение не найдено", status_code=404)
    return offer


class OfferRevalidateView(APIView):
    permission_classes = [require("services.search")]

    def post(self, request, offer_id):
        offer = _get_offer(request, offer_id)
        ctx = AdapterContext(tenant_id=request.user.tenant_id, supplier_id=offer.supplier_id)
        try:
            adapter = get_adapter(offer.provider_adapter or "mock")
            result = adapter.revalidate(ctx, offer.raw_snapshot or {})
        except AdapterError as exc:
            raise ApiError(code=exc.code, message=str(exc), status_code=502) from None
        return Response({"offer": ServiceOfferSerializer(offer).data, "revalidation": result})


class OfferFareRulesView(APIView):
    permission_classes = [require("services.search")]

    def get(self, request, offer_id):
        offer = _get_offer(request, offer_id)
        ctx = AdapterContext(tenant_id=request.user.tenant_id, supplier_id=offer.supplier_id)
        try:
            adapter = get_adapter(offer.provider_adapter or "mock")
            rules = adapter.fare_rules(ctx, offer.raw_snapshot or {})
        except AdapterError as exc:
            raise ApiError(code=exc.code, message=str(exc), status_code=502) from None
        return Response({"fare_rules": rules})


class OfferCompareView(APIView):
    permission_classes = [require("services.search")]

    def post(self, request):
        ids = request.data.get("offer_ids", [])
        if not isinstance(ids, list) or not 2 <= len(ids) <= 5:
            raise ApiError(code="VALIDATION_ERROR", message="Сравнение: от 2 до 5 offer_ids", status_code=400)
        offers = list(ServiceOffer.objects.filter(pk__in=ids, tenant_id=request.user.tenant_id))
        if len(offers) != len(ids):
            raise ApiError(code="NOT_FOUND", message="Часть предложений не найдена", status_code=404)
        return Response({"offers": ServiceOfferSerializer(offers, many=True).data})


class OrderServicesView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination

    def get(self, request, order_id):
        order = get_order_or_404(request.user, order_id)
        qs = order.services.select_related("supplier", "order").prefetch_related("order__participants").order_by("created_at")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(OrderServiceSerializer(page, many=True).data)

    def post(self, request, order_id):
        """Добавляет услугу в заказ: из offer_id (attach) либо вручную."""
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        order = get_order_or_404(request.user, order_id)
        offer_id = request.data.get("offer_id")
        if offer_id:
            offer = _get_offer(request, offer_id)
            if offer.expires_at and offer.expires_at < timezone.now():
                raise ApiError(
                    code="OFFER_EXPIRED",
                    message="Предложение устарело; выполните revalidate",
                    status_code=409,
                )
            service = OrderService.objects.create(
                tenant_id=order.tenant_id,
                order=order,
                kind=offer.kind,
                title=_offer_title(offer),
                supplier=offer.supplier,
                source=OrderService.Source.MANUAL if offer.is_manual else OrderService.Source.API,
                currency=offer.price_currency,
                supplier_cost=offer.price_amount,
                client_total=offer.price_amount,
                provider_snapshot=offer.raw_snapshot,
                policy_compliance=offer.compliance,
                created_by=request.user,
            )
        else:
            serializer = OrderServiceSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            service = serializer.save(
                tenant_id=order.tenant_id,
                order=order,
                source=OrderService.Source.MANUAL,
                created_by=request.user,
            )
        emit_event("service.updated", service, payload={"action": "created", "order_id": str(order.id)})
        audit("services.created", actor=request.user, resource=service, request=request)
        return Response(OrderServiceSerializer(service).data, status=http.HTTP_201_CREATED)


def _offer_title(offer: ServiceOffer) -> str:
    itinerary = offer.itinerary or {}
    if offer.kind == "avia" and itinerary.get("segments"):
        seg = itinerary["segments"][0]
        return f"{seg.get('origin')}–{seg.get('destination')} {seg.get('airline')}{seg.get('flight_number')}"
    if offer.kind == "hotel":
        return itinerary.get("property_name", "Гостиница")
    return itinerary.get("description", offer.kind)


def _get_service(request, service_id) -> OrderService:
    service = OrderService.objects.filter(pk=service_id, tenant_id=request.user.tenant_id).first()
    if service is None:
        raise ApiError(code="NOT_FOUND", message="Услуга не найдена", status_code=404)
    get_order_or_404(request.user, service.order_id)
    return service


class ServiceTransitionView(APIView):
    permission_classes = [require("orders.change")]

    @idempotent_command("services.transition", required=False)
    def post(self, request, service_id):
        target = str(request.data.get("target_status", ""))
        with transaction.atomic():
            service = OrderService.objects.select_for_update().get(pk=_get_service(request, service_id).pk)
            check_version(service, request.data.get("version"))
            allowed = SERVICE_TRANSITIONS.get(service.status, set())
            if target not in allowed:
                raise TransitionForbiddenError(
                    code="SERVICE_STATUS_TRANSITION_FORBIDDEN",
                    message=f"Переход из {service.status} в {target} запрещён",
                    details={"current_status": service.status, "allowed": sorted(allowed)},
                )
            action_map = {
                "booked": "book",
                "issued": "issue",
                "refund_in_progress": "refund",
                "cancelled": "cancel",
            }
            if action := action_map.get(target):
                if not has_permission(request.user, f"services.{action}"):
                    raise ApiError(
                        code="PERMISSION_DENIED", message=f"Нет права services.{action}", status_code=403
                    )
                if not has_service_action(request.user, service.kind, action):
                    raise ApiError(
                        code="SERVICE_KIND_FORBIDDEN",
                        message=f"Нет доступа к {action} для {service.kind}",
                        status_code=403,
                    )

            if target == "issued" and service.source == OrderService.Source.API and not service.external_id:
                raise ApiError(
                    code="ISSUE_REQUIRES_PROVIDER_RESULT",
                    message="Выписка API-услуги выполняется через booking workflow",
                    status_code=409,
                )
            old_status = service.status
            service.status = target
            service.version += 1
            service.updated_by = request.user
            service.save(update_fields=["status", "version", "updated_by", "updated_at"])
            emit_event(
                "service.updated",
                service,
                payload={"action": "status_changed", "from": old_status, "to": target},
            )
            event = audit(
                "services.status_changed",
                actor=request.user,
                resource=service,
                request=request,
                reason=str(request.data.get("reason", "")),
                before={"status": old_status},
                after={"status": target},
            )
        data = OrderServiceSerializer(service).data
        data["audit_event_id"] = str(event.id)
        return Response(data)


class ServiceExtrasView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, service_id):
        service = _get_service(request, service_id)
        return Response(ServiceExtraSerializer(service.extras.all(), many=True).data)

    def post(self, request, service_id):
        if not has_permission(request.user, "orders.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права orders.change", status_code=403)
        service = _get_service(request, service_id)
        serializer = ServiceExtraSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        extra = serializer.save(tenant_id=service.tenant_id, service=service, created_by=request.user)
        emit_event("service.updated", service, payload={"action": "extra_added"})
        return Response(ServiceExtraSerializer(extra).data, status=http.HTTP_201_CREATED)


class ServiceResponsibleView(APIView):
    permission_classes = [require("orders.reassign", "orders.change")]

    def put(self, request, service_id):
        from accounts.models import User

        service = _get_service(request, service_id)
        user = User.objects.filter(
            pk=request.data.get("responsible"), tenant_id=request.user.tenant_id, status=User.Status.ACTIVE
        ).first()
        if user is None:
            raise ApiError(code="VALIDATION_ERROR", message="Пользователь не найден", status_code=400)
        previous = service.responsible
        service.responsible = user
        service.version += 1
        service.updated_by = request.user
        service.save(update_fields=["responsible", "version", "updated_by", "updated_at"])
        audit(
            "services.responsible_changed",
            actor=request.user,
            resource=service,
            request=request,
            before={"responsible": str(previous.pk) if previous else None},
            after={"responsible": str(user.pk)},
        )
        return Response(OrderServiceSerializer(service).data)


class ManualOfferCreateView(APIView):
    """Ручной вариант от поставщика — тот же нормализованный ServiceOffer (ТЗ §9.4)."""

    permission_classes = [require("services.search")]

    def post(self, request):
        kind = str(request.data.get("kind", ""))
        itinerary = request.data.get("itinerary")
        price = request.data.get("price", {})
        if not kind or not isinstance(itinerary, dict) or "amount" not in price:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Нужны kind, itinerary и price{amount, currency}",
                status_code=400,
            )
        supplier = None
        if supplier_id := request.data.get("supplier"):
            from suppliers.models import Supplier

            supplier = Supplier.objects.filter(pk=supplier_id, tenant_id=request.user.tenant_id).first()
        offer = ServiceOffer.objects.create(
            tenant_id=request.user.tenant_id,
            kind=kind,
            supplier=supplier,
            is_manual=True,
            itinerary=itinerary,
            fare=request.data.get("fare"),
            price_amount=price["amount"],
            price_currency=price.get("currency", "USD"),
            availability="manual",
            created_by=request.user,
        )
        audit("services.manual_offer_created", actor=request.user, resource=offer, request=request)
        return Response(ServiceOfferSerializer(offer).data, status=http.HTTP_201_CREATED)
