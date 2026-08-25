from django.db import migrations


ROLE_CODES = ("admin", "operator", "accountant", "manager")
REQUIRED_PERMISSIONS = ("documents.view", "documents.upload")


def grant_receipt_editor_permissions(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")
    for role in Role.objects.filter(code__in=ROLE_CODES):
        existing = set(
            RolePermission.objects.filter(role=role).values_list("permission_code", flat=True)
        )
        RolePermission.objects.bulk_create(
            [
                RolePermission(role=role, permission_code=permission)
                for permission in REQUIRED_PERMISSIONS
                if permission not in existing
            ]
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_demoaccessrequest")]

    operations = [
        migrations.RunPython(
            grant_receipt_editor_permissions,
            migrations.RunPython.noop,
        ),
    ]
