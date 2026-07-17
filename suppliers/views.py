import json

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from common.jobs import enqueue
from common.pagination import DefaultPagination
from suppliers.models import Supplier, SupplierCredential, SupplierMarkupRule, SupplierSearchPriority


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "legal_name",
            "status",
            "organization_type",
            "is_global",
            "service_kinds",
            "countries",
            "cities",
            "currencies",
            "communication_methods",
            "work_hours",
            "settlement_type",
            "contract_number",
            "contact_person",
            "phone",
            "email",
            "automation_capabilities",
            "created_at",
            "version",
        ]
        read_only_fields = ["id", "created_at", "version"]


class CredentialSerializer(serializers.ModelSerializer):
    """Секреты принимаются на запись, наружу не возвращаются (ТЗ §21.2)."""

    secrets = serializers.DictField(write_only=True, required=False)
    has_secrets = serializers.SerializerMethodField()

    class Meta:
        model = SupplierCredential
        fields = [
            "id",
            "provider_adapter",
            "environment",
            "secrets",
            "has_secrets",
            "status",
            "last_verified_at",
            "rotated_at",
        ]
        read_only_fields = ["id", "status", "last_verified_at", "rotated_at"]

    def get_has_secrets(self, obj) -> bool:
        return bool(obj.encrypted_secrets)


class MarkupRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierMarkupRule
        fields = [
            "id",
            "service_kind",
            "route",
            "geography",
            "airline",
            "cabin",
            "passenger_category",
            "amount_type",
            "amount_value",
            "currency",
            "priority",
            "effective_from",
            "effective_to",
        ]
        read_only_fields = ["id"]


class SearchPrioritySerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierSearchPriority
        fields = ["id", "service_kind", "ordered_suppliers", "conditions", "fallback_supplier", "is_active"]
        read_only_fields = ["id"]


def _get_supplier(request, supplier_id) -> Supplier:
    supplier = Supplier.objects.filter(
        pk=supplier_id, tenant_id=request.user.tenant_id, archived_at__isnull=True
    ).first()
    if supplier is None:
        raise ApiError(code="NOT_FOUND", message="Поставщик не найден", status_code=404)
    return supplier


class SupplierListCreateView(GenericAPIView):
    permission_classes = [require("suppliers.view")]
    pagination_class = DefaultPagination
    serializer_class = SupplierSerializer

    def get(self, request):
        qs = Supplier.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True).order_by(
            "name"
        )
        params = request.query_params
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(name__icontains=q) | Q(legal_name__icontains=q))
        if supplier_status := params.get("status"):
            qs = qs.filter(status=supplier_status)
        if kind := params.get("service_kind"):
            qs = qs.filter(service_kinds__contains=[kind])
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(SupplierSerializer(page, many=True).data)

    def post(self, request):
        self.permission_classes = [require("suppliers.change")]
        self.check_permissions(request)
        serializer = SupplierSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplier = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        audit("suppliers.created", actor=request.user, resource=supplier, request=request)
        return Response(SupplierSerializer(supplier).data, status=http.HTTP_201_CREATED)


class SupplierDetailView(APIView):
    permission_classes = [require("suppliers.view")]

    def get(self, request, supplier_id):
        return Response(SupplierSerializer(_get_supplier(request, supplier_id)).data)

    def patch(self, request, supplier_id):
        self.permission_classes = [require("suppliers.change")]
        self.check_permissions(request)
        supplier = _get_supplier(request, supplier_id)
        serializer = SupplierSerializer(supplier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user, version=supplier.version + 1)
        audit("suppliers.updated", actor=request.user, resource=supplier, request=request)
        return Response(serializer.data)


class SupplierCredentialsView(APIView):
    """API credentials видит только admin с integrations.manage (ТЗ §5.3)."""

    permission_classes = [require("integrations.manage")]

    def get(self, request, supplier_id):
        supplier = _get_supplier(request, supplier_id)
        credentials = supplier.credentials.filter(archived_at__isnull=True)
        return Response(CredentialSerializer(credentials, many=True).data)

    def post(self, request, supplier_id):
        supplier = _get_supplier(request, supplier_id)
        serializer = CredentialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        secrets = serializer.validated_data.pop("secrets", {})
        credential = SupplierCredential(
            tenant_id=request.user.tenant_id,
            supplier=supplier,
            created_by=request.user,
            rotated_by=request.user,
            rotated_at=timezone.now(),
            **serializer.validated_data,
        )
        if secrets:
            credential.encrypted_secrets = json.dumps(secrets, ensure_ascii=False)
        credential.save()

        audit(
            "suppliers.credential_created",
            actor=request.user,
            resource=supplier,
            request=request,
            after={"provider_adapter": credential.provider_adapter, "environment": credential.environment},
        )
        return Response(CredentialSerializer(credential).data, status=http.HTTP_201_CREATED)


class SupplierCheckConnectionView(APIView):
    permission_classes = [require("integrations.manage")]

    def post(self, request, supplier_id):
        supplier = _get_supplier(request, supplier_id)
        job = enqueue("suppliers.check_connection", {"supplier_id": str(supplier.id)}, request=request)
        return Response({"job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)


class SupplierMarkupRulesView(APIView):
    permission_classes = [require("suppliers.manage_markup")]

    def get(self, request, supplier_id):
        supplier = _get_supplier(request, supplier_id)
        rules = supplier.markup_rules.filter(archived_at__isnull=True).order_by("priority")
        return Response(MarkupRuleSerializer(rules, many=True).data)

    def post(self, request, supplier_id):
        supplier = _get_supplier(request, supplier_id)
        serializer = MarkupRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save(tenant_id=request.user.tenant_id, supplier=supplier, created_by=request.user)
        audit("suppliers.markup_rule_created", actor=request.user, resource=supplier, request=request)
        return Response(MarkupRuleSerializer(rule).data, status=http.HTTP_201_CREATED)


class SearchPriorityListCreateView(APIView):
    permission_classes = [require("suppliers.view")]

    def get(self, request):
        priorities = SupplierSearchPriority.objects.filter(
            tenant_id=request.user.tenant_id, archived_at__isnull=True
        )
        return Response(SearchPrioritySerializer(priorities, many=True).data)

    def post(self, request):
        self.permission_classes = [require("settings.manage")]
        self.check_permissions(request)
        serializer = SearchPrioritySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        SupplierSearchPriority.objects.filter(
            tenant_id=request.user.tenant_id,
            service_kind=serializer.validated_data["service_kind"],
            is_active=True,
        ).update(is_active=False)
        priority = serializer.save(tenant_id=request.user.tenant_id, created_by=request.user)
        audit("suppliers.search_priority_changed", actor=request.user, request=request, resource=priority)
        return Response(SearchPrioritySerializer(priority).data, status=http.HTTP_201_CREATED)
