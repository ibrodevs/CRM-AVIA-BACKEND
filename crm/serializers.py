from rest_framework import serializers

from accounts.permissions import has_permission
from common.fields import mask_tail
from crm.models import (
    Agreement, ClientProfile, Company, CompanyContact, Contract, Department, Employee,
    FeeRule, FeeTemplate, LoyaltyCard, Person, PersonDocument, SettlementProfile,
)


class PersonSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Person
        fields = ["id", "surname", "given_name", "middle_name", "full_name",
                  "latin_surname", "latin_given_name", "birth_date", "gender",
                  "citizenship", "phone", "email", "city", "preferred_language",
                  "preferred_channel", "status", "notes", "created_at", "version"]
        read_only_fields = ["id", "created_at", "version"]


class PersonDocumentSerializer(serializers.ModelSerializer):
    number = serializers.CharField(write_only=True)
    number_masked = serializers.SerializerMethodField()

    class Meta:
        model = PersonDocument
        fields = ["id", "type", "number", "number_masked", "series", "issued_at",
                  "expires_at", "issuing_country", "issuing_authority", "nationality",
                  "verified_at", "status", "created_at"]
        read_only_fields = ["id", "verified_at", "created_at"]

    def get_number_masked(self, obj) -> str:
        request = self.context.get("request")
        # Полный номер — только с отдельным правом (ТЗ §5.3).
        if request and has_permission(request.user, "crm.view_person_documents"):
            return obj.number
        return mask_tail(obj.number)


class LoyaltyCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltyCard
        fields = ["id", "program_type", "provider", "number", "status", "auto_apply",
                  "valid_until", "metadata"]
        read_only_fields = ["id"]


class ClientProfileSerializer(serializers.ModelSerializer):
    person_detail = PersonSerializer(source="person", read_only=True)

    class Meta:
        model = ClientProfile
        fields = ["id", "person", "person_detail", "client_type", "status", "source",
                  "assigned_manager", "created_at"]
        read_only_fields = ["id", "created_at"]


class CompanySerializer(serializers.ModelSerializer):
    bank_account_masked = serializers.SerializerMethodField()
    bank_account = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Company
        fields = ["id", "legal_name", "short_name", "type", "status", "tax_id", "okpo",
                  "vat_mode", "legal_address", "bank_name", "bank_account",
                  "bank_account_masked", "director", "phone", "email",
                  "requires_e_sign", "assigned_manager", "created_at", "version"]
        read_only_fields = ["id", "created_at", "version"]

    def get_bank_account_masked(self, obj) -> str:
        return mask_tail(obj.bank_account or "")


class CompanyContactSerializer(serializers.ModelSerializer):
    person_detail = PersonSerializer(source="person", read_only=True)

    class Meta:
        model = CompanyContact
        fields = ["id", "person", "person_detail", "role", "is_primary"]
        read_only_fields = ["id"]


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "parent", "travel_policy"]
        read_only_fields = ["id"]


class EmployeeSerializer(serializers.ModelSerializer):
    person_detail = PersonSerializer(source="person", read_only=True)

    class Meta:
        model = Employee
        fields = ["id", "person", "person_detail", "personnel_number", "department",
                  "position", "status"]
        read_only_fields = ["id"]


class FeeRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeeRule
        fields = ["id", "service_kind", "fee_kind", "calculation", "value", "currency",
                  "description"]
        read_only_fields = ["id"]


class FeeTemplateSerializer(serializers.ModelSerializer):
    rules = FeeRuleSerializer(many=True, read_only=True)

    class Meta:
        model = FeeTemplate
        fields = ["id", "name", "description", "rules"]
        read_only_fields = ["id"]


class AgreementSerializer(serializers.ModelSerializer):
    fee_rules = FeeRuleSerializer(many=True, read_only=True)

    class Meta:
        model = Agreement
        fields = ["id", "number", "agreement_version", "status", "effective_from",
                  "effective_to", "fee_template", "service_descriptions",
                  "fee_descriptions", "fee_rules"]
        read_only_fields = ["id", "agreement_version"]


class ContractSerializer(serializers.ModelSerializer):
    agreements = AgreementSerializer(many=True, read_only=True)

    class Meta:
        model = Contract
        fields = ["id", "number", "signed_at", "starts_at", "ends_at", "status",
                  "agreements"]
        read_only_fields = ["id"]


class SettlementProfileSerializer(serializers.ModelSerializer):
    available_deposit = serializers.SerializerMethodField()

    class Meta:
        model = SettlementProfile
        fields = ["mode", "currency", "deposit_balance", "deposit_reserved",
                  "available_deposit", "credit_limit", "credit_days"]
        read_only_fields = ["deposit_balance", "deposit_reserved"]

    def get_available_deposit(self, obj) -> str:
        return str(obj.deposit_balance - obj.deposit_reserved)
