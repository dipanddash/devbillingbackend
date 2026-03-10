from django.db import migrations


def normalize_user_role(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(role__isnull=True):
        normalized = (user.role or "").upper()
        if user.role != normalized:
            user.role = normalized
            user.save(update_fields=["role"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_user_day_locked_on"),
    ]

    operations = [
        migrations.RunPython(normalize_user_role, migrations.RunPython.noop),
    ]
