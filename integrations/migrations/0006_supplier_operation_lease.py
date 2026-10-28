from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("integrations", "0005_shared_cache")]
    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameModel("HotelbookOperationLease", "SupplierOperationLease"),
                migrations.AlterModelTable("SupplierOperationLease", "integrations_hotelbookoperationlease"),
            ]
        )
    ]
