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
            "max",
            "whatsapp",
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

    def validate_timezone(self, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Неизвестный часовой пояс") from None
        return value

    def get_roles(self, obj) -> list[str]:
        return [ur.role.code for ur in obj.user_roles.all()]


class MeSerializer(UserSerializer):
    avatar = serializers.SerializerMethodField()
    service_access = serializers.SerializerMethodField()
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default="")
    preferences = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["permissions", "service_access", "manager_name", "preferences"]

    def get_permissions(self, obj) -> list[str]:
        return sorted(user_permission_codes(obj))

    def get_avatar(self, obj):
        return f"/api/v1/me/avatar/?v={obj.avatar.name}" if obj.avatar else None

    def get_service_access(self, obj):
        return UserServiceAccessSerializer(obj.service_access.all(), many=True).data

    def get_preferences(self, obj):
        preference, _ = UserPreference.objects.get_or_create(user=obj)
        return UserPreferenceSerializer(preference).data


class UserPreferenceSerializer(serializers.ModelSerializer):
    theme = serializers.ChoiceField(choices=["light", "dark", "system"], required=False)
    date_format = serializers.ChoiceField(choices=["DD.MM.YYYY", "MM/DD/YYYY", "YYYY-MM-DD"], required=False)
    time_format = serializers.ChoiceField(choices=["24h", "12h"], required=False)
    language = serializers.ChoiceField(choices=["ru", "ky", "en"], required=False)
    page_size = serializers.ChoiceField(choices=[10, 25, 50, 100], required=False)
    start_page = serializers.ChoiceField(choices=["dashboard", "orders", "fulfillment", "chats"], required=False)

    def to_internal_value(self, data):
        data = data.copy()
        aliases = {
            "date_format": {"ДД.ММ.ГГГГ": "DD.MM.YYYY", "ММ/ДД/ГГГГ": "MM/DD/YYYY", "ГГГГ-ММ-ДД": "YYYY-MM-DD"},
            "time_format": {"24 часа": "24h", "12 часов (AM/PM)": "12h"},
            "start_page": {"Главное": "dashboard", "Заказы": "orders", "Оформление": "fulfillment", "Чаты": "chats"},
        }
        for field, values in aliases.items():
            if field in data and isinstance(data[field], str):
                data[field] = values.get(data[field], data[field])
        return super().to_internal_value(data)

    def validate_base_currency(self, value):
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise serializers.ValidationError("Ожидается трёхбуквенный код валюты")
        return value.upper()

    def validate_notification_channels(self, value):
        return self._booleans(value)

    def validate_notification_categories(self, value):
        return self._booleans(value)

    @staticmethod
    def _booleans(value):
        if not isinstance(value, dict) or any(not isinstance(v, bool) for v in value.values()):
            raise serializers.ValidationError("Ожидается объект с переключателями true/false")
        return value

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
    service_kind = serializers.ChoiceField(choices=["avia", "rail", "hotel", "transfer", "bus", "tour", "visa", "insurance", "aeroexpress", "lounge", "other"])
    allowed_actions = serializers.ListField(child=serializers.ChoiceField(choices=["view", "search", "book", "issue", "refund", "exchange", "cancel", "correct_document", "send_document", "extras"]), allow_empty=True)

    class Meta:
        model = UserServiceAccess
        fields = ["service_kind", "allowed_actions"]


class UserCreateSerializer(serializers.ModelSerializer):
    roles = serializers.ListField(child=serializers.CharField(), required=False, write_only=True)
    status = serializers.ChoiceField(choices=["invited", "active", "suspended"], required=False)
    password = serializers.CharField(required=False, write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = [
            "email",
            "roles",
            "status",
            "password",
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
