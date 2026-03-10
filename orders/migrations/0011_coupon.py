from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0010_alter_order_order_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="Coupon",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(db_index=True, max_length=40, unique=True)),
                ("discount_type", models.CharField(choices=[("AMOUNT", "Amount"), ("PERCENT", "Percent")], max_length=10)),
                ("value", models.DecimalField(decimal_places=2, max_digits=10)),
                ("min_order_amount", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("max_discount_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("valid_from", models.DateTimeField(blank=True, null=True)),
                ("valid_to", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["is_active", "code"], name="orders_coupo_is_acti_e038b6_idx")],
            },
        ),
    ]
