from django.db import migrations


def backfill_order_numbers(apps, schema_editor):
    Order = apps.get_model("orders", "Order")

    next_number = 1
    for order in Order.objects.all().order_by("created_at", "id"):
        if order.order_number is None:
            order.order_number = next_number
            order.save(update_fields=["order_number"])
        next_number = max(next_number, (order.order_number or 0) + 1)


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0007_order_order_number"),
    ]

    operations = [
        migrations.RunPython(backfill_order_numbers, migrations.RunPython.noop),
    ]
