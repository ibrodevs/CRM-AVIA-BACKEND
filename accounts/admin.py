from django.contrib import admin

from accounts.models import Role, RolePermission, User, UserRole, UserSession


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["email", "get_full_name", "status", "position", "tenant", "last_login"]
    list_filter = ["status", "tenant"]
    search_fields = ["email", "first_name", "last_name"]
    readonly_fields = ["last_login", "created_at", "updated_at", "password"]
    exclude: list[str] = []


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "tenant", "is_system"]
    inlines = [RolePermissionInline]


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ["user", "role", "assigned_at", "assigned_by"]


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ["user", "ip_address", "created_at", "last_seen_at", "revoked_at"]
    readonly_fields = [f.name for f in UserSession._meta.fields]

    def has_add_permission(self, request):  # noqa: ARG002
        return False
