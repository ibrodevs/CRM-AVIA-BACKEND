from rest_framework import serializers

from accounts.models import Role, User, UserPreference, UserServiceAccess
from accounts.permissions import user_permission_codes


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ["id", "code", "name", "description", "is_system", "permissions"]

    def get_permissions(self, obj) -> list[str]:
        return sorted(obj.permissions.values_list("permission_code", flat=True))


class UserBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "first_name", "last_name", "position", "presence"]


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "status",
            "first_name",
            "last_name",
            "middle_name",
            "full_name",
            "avatar",
            "position",
            "department",
            "manager",
            "work_phone",
            "internal_phone",
            "telegram",
            "hired_at",
            "work_status",
            "presence",
            "timezone",
            "language",
            "sla_response_minutes",
            "last_login",
            "created_at",
            "roles",
        ]
        read_only_fields = ["id", "email", "status", "last_login", "created_at"]

    def get_roles(self, obj) -> list[str]:
        return [ur.role.code for ur in obj.user_roles.all()]


class MeSerializer(UserSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["permissions"]

    def get_permissions(self, obj) -> list[str]:
        return sorted(user_permission_codes(obj))


class UserPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPreference
        fields = [
            "theme",
            "date_format",
            "time_format",
            "base_currency",
            "language",
            "page_size",
            "start_page",
            "notification_channels",
            "notification_categories",
        ]


class UserServiceAccessSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserServiceAccess
        fields = ["service_kind", "allowed_actions"]


class UserCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "email",
            "phone",
            "first_name",
            "last_name",
            "middle_name",
            "position",
            "department",
            "manager",
            "timezone",
            "language",
        ]
