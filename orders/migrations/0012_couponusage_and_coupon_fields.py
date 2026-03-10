from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0011_coupon"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="coupon",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="coupon",
            name="first_time_only",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="coupon",
            name="free_item",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="coupon",
            name="free_item_category",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="coupon",
            name="max_uses",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="coupon",
            name="discount_type",
            field=models.CharField(choices=[("AMOUNT", "Amount"), ("PERCENT", "Percent"), ("FREE_ITEM", "Free Item")], max_length=10),
        ),
        migrations.CreateModel(
            name="CouponUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("customer_phone", models.CharField(blank=True, default="", max_length=20)),
                ("discount_amount", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("used_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("coupon", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_records", to="orders.coupon")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="coupon_usages", to="orders.order")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-used_at"],
                "unique_together": {("coupon", "order")},
            },
        ),
    ]
