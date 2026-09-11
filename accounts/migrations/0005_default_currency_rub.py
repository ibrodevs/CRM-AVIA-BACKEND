from django.db import migrations, models


class Migration(migrations.Migration):
    """Валюта по умолчанию — рубль.

    Интерфейс и расчёты рассчитаны на рублёвый рынок, а прежний дефолт USD
    приводил к долларовым суммам у пользователей, которые валюту не выбирали.
    Уже сохранённые предпочтения не трогаем: выбор пользователя важнее дефолта.
    """

    dependencies = [("accounts", "0004_profile_contacts")]

    operations = [
        migrations.AlterField(
            model_name="userpreference",
            name="base_currency",
            field=models.CharField(default="RUB", max_length=3),
        ),
    ]
