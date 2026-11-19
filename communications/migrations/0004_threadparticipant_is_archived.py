from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("communications", "0003_message_is_internal")]
    operations = [migrations.AddField(model_name="threadparticipant", name="is_archived", field=models.BooleanField(default=False))]
