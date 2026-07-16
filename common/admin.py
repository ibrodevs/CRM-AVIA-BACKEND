from django.contrib import admin

from common.models import AuditEvent, BackgroundJob, OutboxEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["action", "actor", "resource_type", "resource_id", "occurred_at"]
    list_filter = ["action"]
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):  # noqa: ARG002
        return False

    def has_change_permission(self, request, obj=None):  # noqa: ARG002
        return False

    def has_delete_permission(self, request, obj=None):  # noqa: ARG002
        return False


@admin.register(BackgroundJob)
class BackgroundJobAdmin(admin.ModelAdmin):
    list_display = ["kind", "status", "attempts", "run_after", "created_at", "completed_at"]
    list_filter = ["status", "kind"]
    readonly_fields = [f.name for f in BackgroundJob._meta.fields]


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ["id", "event_type", "resource_type", "resource_id", "occurred_at", "processed_at"]
    list_filter = ["event_type"]
    readonly_fields = [f.name for f in OutboxEvent._meta.fields]

    def has_add_permission(self, request):  # noqa: ARG002
        return False
