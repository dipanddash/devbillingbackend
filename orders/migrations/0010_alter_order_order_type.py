from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0009_alter_order_bill_number_alter_order_created_at_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="order_type",
            field=models.CharField(
                choices=[
                    ("DINE_IN", "Dine In"),
                    ("TAKEAWAY", "Takeaway"),
                    ("SWIGGY", "Swiggy"),
                    ("ZOMATO", "Zomato"),
                ],
                max_length=15,
            ),
        ),
    ]
