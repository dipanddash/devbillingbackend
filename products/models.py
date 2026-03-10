import uuid
from django.db import models

class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    image = models.ImageField(
        upload_to="categories/",
        null=True,
        blank=True
    )

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150)
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products"
    )

    price = models.DecimalField(max_digits=10, decimal_places=2)

    # ✅ ADD THIS
    gst_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00
    )

    image = models.ImageField(
        upload_to="products/",
        null=True,
        blank=True
    )

    is_active = models.BooleanField(default=True, db_index=True)

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Addon(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150)
    price = models.DecimalField(max_digits=8, decimal_places=2)

    image = models.ImageField(
        upload_to="addons/",
        null=True,
        blank=True
    )

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class ProductAddon(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)

class Recipe(models.Model):

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="recipes"
    )

    ingredient = models.ForeignKey(
        "inventory.Ingredient",
        on_delete=models.CASCADE
    )

    quantity = models.DecimalField(max_digits=10, decimal_places=3)

    def __str__(self):
        return f"{self.product.name} → {self.ingredient.name} ({self.quantity})"

# -------------------------
# COMBO
# -------------------------

class Combo(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    name = models.CharField(
        max_length=150,
        unique=True
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    gst_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00
    )

    image = models.ImageField(
        upload_to="combos/",
        null=True,
        blank=True
    )

    is_active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


# -------------------------
# COMBO ITEMS
# -------------------------

class ComboItem(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    combo = models.ForeignKey(
        Combo,
        on_delete=models.CASCADE,
        related_name="items"
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="combo_products"
    )

    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("combo", "product")
        ordering = ["combo"]

    def __str__(self):
        return f"{self.combo.name} - {self.product.name} x {self.quantity}"
