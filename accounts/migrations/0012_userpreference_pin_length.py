from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0011_userpreference_auto_lock_minutes_and_more')]
    operations = [migrations.AddField(
        model_name='userpreference', name='pin_length',
        field=models.PositiveSmallIntegerField(default=6),
    )]
