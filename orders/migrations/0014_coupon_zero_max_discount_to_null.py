from django.db import migrations


def normalize_coupon_max_discount(apps, schema_editor):
    Coupon = apps.get_model("orders", "Coupon")
    Coupon.objects.filter(max_discount_amount__isnull=False, max_discount_amount__lte=0).update(
        max_discount_amount=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0013_rename_orders_coupo_is_acti_e038b6_idx_orders_coup_is_acti_db61a9_idx"),
    ]

    operations = [
        migrations.RunPython(normalize_coupon_max_discount, migrations.RunPython.noop),
    ]
