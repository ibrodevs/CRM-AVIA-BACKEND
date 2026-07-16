"""Travel policy API (ТЗ §6.3, §6.4)."""
from rest_framework import serializers, status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from crm.models import Company
from travel_policy.models import TravelPolicy, check_offer_compliance


class TravelPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = TravelPolicy
        fields = ["id", "company", "name", "effective_from", "effective_to", "is_active",
                  "policy_version", "scopes", "allowed_avia_cabins", "allowed_airlines",
                  "allowed_rail_classes", "allowed_train_types", "allowed_hotel_categories",
                  "allowed_hotel_chains", "allowed_meal_plans", "allowed_car_classes",
                  "price_limits", "min_advance_booking_days", "approver_chain"]
        read_only_fields = ["id", "policy_version", "company"]


class CompanyTravelPoliciesView(APIView):
    permission_classes = [require("crm.view")]

    def get(self, request, company_id):
        policies = TravelPolicy.objects.filter(
            company_id=company_id, tenant_id=request.user.tenant_id, archived_at__isnull=True
        )
        return Response(TravelPolicySerializer(policies, many=True).data)

    def post(self, request, company_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        company = Company.objects.filter(pk=company_id,
                                         tenant_id=request.user.tenant_id).first()
        if company is None:
            raise ApiError(code="NOT_FOUND", message="Компания не найдена", status_code=404)
        serializer = TravelPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = serializer.save(tenant_id=request.user.tenant_id, company=company,
                                 created_by=request.user)
        audit("travel_policy.created", actor=request.user, resource=policy, request=request)
        return Response(TravelPolicySerializer(policy).data, status=http.HTTP_201_CREATED)


class TravelPolicyDetailView(APIView):
    permission_classes = [require("crm.view")]

    def _get(self, request, policy_id) -> TravelPolicy:
        policy = TravelPolicy.objects.filter(pk=policy_id,
                                             tenant_id=request.user.tenant_id).first()
        if policy is None:
            raise ApiError(code="NOT_FOUND", message="Политика не найдена", status_code=404)
        return policy

    def get(self, request, policy_id):
        return Response(TravelPolicySerializer(self._get(request, policy_id)).data)

    def patch(self, request, policy_id):
        if not has_permission(request.user, "crm.change"):
            raise ApiError(code="PERMISSION_DENIED", message="Нет права crm.change",
                           status_code=403)
        policy = self._get(request, policy_id)
        serializer = TravelPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(policy_version=policy.policy_version + 1, updated_by=request.user)
        audit("travel_policy.updated", actor=request.user, resource=policy, request=request)
        return Response(serializer.data)


class TravelPolicyCheckView(APIView):
    """POST /travel-policies/{id}/check/ — compliance оффера (ТЗ §6.3)."""

    permission_classes = [require("crm.view")]

    def post(self, request, policy_id):
        policy = TravelPolicy.objects.filter(pk=policy_id,
                                             tenant_id=request.user.tenant_id).first()
        if policy is None:
            raise ApiError(code="NOT_FOUND", message="Политика не найдена", status_code=404)
        offer = request.data.get("offer")
        if not isinstance(offer, dict):
            raise ApiError(code="VALIDATION_ERROR", message="Ожидается объект offer",
                           fields={"offer": ["Обязательное поле-объект"]}, status_code=400)
        result = check_offer_compliance(policy, offer)
        return Response(result.as_dict(policy))
