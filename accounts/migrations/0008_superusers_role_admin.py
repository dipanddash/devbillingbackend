from django.db import migrations


def set_superusers_admin_role(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).update(role="ADMIN")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_normalize_user_role"),
    ]

    operations = [
        migrations.RunPython(set_superusers_admin_role, migrations.RunPython.noop),
    ]
