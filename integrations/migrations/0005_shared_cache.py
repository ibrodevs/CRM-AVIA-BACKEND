from django.core.management import call_command
from django.db import migrations


def create_shared_cache(apps, schema_editor):
    call_command("createcachetable", "crm_shared_cache", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("integrations", "0004_hbpro_operation_lease")]
    operations = [migrations.RunPython(create_shared_cache, migrations.RunPython.noop)]
