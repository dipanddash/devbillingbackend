from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0004_ingredientcategory_ingredient_category"),
        ("products", "0004_alter_combo_created_at_alter_combo_is_active_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="addon",
            name="ingredient",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_addons",
                to="inventory.ingredient",
            ),
        ),
        migrations.AddField(
            model_name="addon",
            name="ingredient_quantity",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=10),
        ),
    ]
