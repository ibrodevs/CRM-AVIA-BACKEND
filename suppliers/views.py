import json

from django.db import transaction
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
from common.pagination import DefaultPagination
from suppliers.job_handlers import verify_supplier_credentials
from suppliers.models import Supplier, SupplierCredential, SupplierMarkupRule, SupplierSearchPriority


class SupplierSerializer(serializers.ModelSerializer):
    metrics = serializers.SerializerMethodField()

    def get_metrics(self, obj):
        from crm.entity_metrics import supplier_metrics
        return supplier_metrics(obj)

    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "metrics",
            "legal_name",
            "tax_id",
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
        result = verify_supplier_credentials(supplier)
        audit(
            "suppliers.connection_checked",
            actor=request.user,
            resource=supplier,
            request=request,
            after={"status": result["status"]},
        )
        response_status = http.HTTP_200_OK if result["status"] == "connected" else http.HTTP_409_CONFLICT
        return Response(result, status=response_status)


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


class SupplierSettingsView(APIView):
    permission_classes = [require('suppliers.view')]

    def get(self, request, supplier_id):
        from common.models import WorkspaceSetting
        supplier = _get_supplier(request, supplier_id)
        namespace = f'supplier-ext-{supplier.id}'
        setting = WorkspaceSetting.objects.filter(tenant_id=supplier.tenant_id, owner__isnull=True, namespace=namespace).first()
        if setting is None:
            setting = WorkspaceSetting.objects.filter(tenant_id=supplier.tenant_id, owner=request.user, namespace=namespace).first()
        value = dict(setting.value) if setting else {}
        if 'api' in value:
            value['api'] = {key: entry for key, entry in value['api'].items() if key in {'url', 'version'}}
        return Response({'value': value})

    def patch(self, request, supplier_id):
        from django.db import transaction

        from common.models import WorkspaceSetting
        self.permission_classes = [require('suppliers.change')]
        self.check_permissions(request)
        supplier = _get_supplier(request, supplier_id)
        value = request.data.get('value')
        if not isinstance(value, dict):
            raise ApiError(code='VALIDATION_ERROR', message='Ожидается объект настроек', status_code=400)
        value = {key: entry for key, entry in value.items() if key in {'supType', 'kinds', 'priority', 'useDefault', 'country', 'city', 'api', 'local', 'fin', 'ops', 'automation', 'searchPriority', 'sla', 'legal'}}
        for field in ('api', 'local', 'fin', 'ops', 'searchPriority', 'sla', 'legal'):
            if field in value and not isinstance(value[field], dict):
                raise ApiError(code='VALIDATION_ERROR', message=f'{field}: ожидается объект', status_code=400)
        # Credentials belong exclusively in encrypted SupplierCredential storage.
        value['api'] = {key: entry for key, entry in value.get('api', {}).items() if key in {'url', 'version'}}
        legal, local, fin = value.get('legal', {}), value.get('local', {}), value.get('fin', {})
        with transaction.atomic():
            fields = {'tax_id': legal.get('inn', supplier.tax_id), 'legal_name': legal.get('legalName', supplier.legal_name), 'contract_number': legal.get('contractNo', supplier.contract_number), 'phone': legal.get('phone', supplier.phone), 'email': legal.get('email', supplier.email), 'contact_person': local.get('contact', supplier.contact_person), 'work_hours': local.get('hours', supplier.work_hours)}
            serializer = SupplierSerializer(supplier, data=fields, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save(updated_by=request.user, automation_capabilities={**supplier.automation_capabilities, 'finance': fin, 'search_mode': value.get('automation', supplier.automation_capabilities.get('search_mode', 'manual')), 'operations': value.get('ops', {}), 'search_priority': value.get('searchPriority', {})})
            WorkspaceSetting.objects.update_or_create(tenant_id=supplier.tenant_id, owner=None, namespace=f'supplier-ext-{supplier.id}', defaults={'value': value, 'updated_by': request.user})
            audit('suppliers.settings_updated', actor=request.user, resource=supplier, request=request)
        return Response({'value': value})


class SupplierAviaMarkupsView(APIView):
    permission_classes = [require("suppliers.view")]

    def get(self, request, supplier_id):
        from common.models import WorkspaceSetting
        supplier = _get_supplier(request, supplier_id)
        setting = WorkspaceSetting.objects.filter(tenant_id=supplier.tenant_id, owner__isnull=True, namespace=f"supplier-avia-{supplier.pk}").first()
        return Response({"value": setting.value.get("config", {}) if setting else {}})

    @transaction.atomic
    def patch(self, request, supplier_id):
        from django.utils import timezone

        from common.models import WorkspaceSetting
        self.permission_classes = [require("suppliers.change")]
        self.check_permissions(request)
        supplier = _get_supplier(request, supplier_id)
        config = request.data.get("value")
        if not isinstance(config, dict) or len(config) > 300:
            raise ApiError(code="VALIDATION_ERROR", message="Некорректные наценки", status_code=400)
        setting, _ = WorkspaceSetting.objects.select_for_update().get_or_create(tenant_id=supplier.tenant_id, owner=None, namespace=f"supplier-avia-{supplier.pk}", defaults={"value": {}})
        ids = []
        for airline, entry in config.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("routes", []), list):
                raise ApiError(code="VALIDATION_ERROR", message="Некорректные наценки", status_code=400)
            buckets = [(key, entry.get(key, {})) for key in ("domestic", "intl")]
            buckets += [("route", row) for row in entry.get("routes", [])]
            for bucket, row in buckets:
                if not isinstance(row, dict):
                    raise ApiError(code="VALIDATION_ERROR", message="Некорректная наценка", status_code=400)
                if bucket == "route" and (not row.get("from") or not row.get("to")):
                    raise ApiError(code="VALIDATION_ERROR", message="Укажите оба аэропорта маршрута", status_code=400)
                data = {"service_kind": "avia", "airline": airline, "geography": bucket if bucket != "route" else "", "route": f"{row.get('from')}-{row.get('to')}" if bucket == "route" else "", "amount_type": row.get("type", "percent"), "amount_value": row.get("value", 0), "currency": "USD", "priority": 10 if bucket == "route" else 100}
                serializer = MarkupRuleSerializer(data=data)
                serializer.is_valid(raise_exception=True)
                rule = serializer.save(tenant_id=supplier.tenant_id, supplier=supplier, created_by=request.user)
                ids.append(str(rule.pk))
        supplier.markup_rules.filter(pk__in=setting.value.get("rule_ids", [])).update(archived_at=timezone.now())
        setting.value = {"config": config, "rule_ids": ids}
        setting.save(update_fields=["value", "updated_at"])
        audit("suppliers.avia_markups_saved", actor=request.user, resource=supplier, request=request)
        return Response({"value": config})
