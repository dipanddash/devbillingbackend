from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_staffsessionlog_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffReportAccess",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("allowed_reports", models.JSONField(blank=True, default=list)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "staff_user",
                    models.OneToOneField(
                        limit_choices_to={"role": "STAFF"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="report_access",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["staff_user__username"],
            },
        ),
    ]
