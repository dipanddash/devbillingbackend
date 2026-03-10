from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_staffreportaccess"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="day_locked_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]

