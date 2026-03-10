from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_user_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffsessionlog",
            name="source",
            field=models.CharField(
                choices=[("SYSTEM_LOGIN", "System Login"), ("ATTENDANCE_DESK", "Attendance Desk")],
                default="SYSTEM_LOGIN",
                max_length=20,
            ),
        ),
    ]

