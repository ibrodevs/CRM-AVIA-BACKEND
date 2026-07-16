from django.contrib import admin

from tenancy.models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "base_currency", "timezone", "is_active"]
