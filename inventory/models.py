import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings

DEFAULT_INGREDIENT_CATEGORY_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class IngredientCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Ingredient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, unique=True)
    category = models.ForeignKey(
        IngredientCategory,
        on_delete=models.PROTECT,
        related_name="ingredients",
        default=DEFAULT_INGREDIENT_CATEGORY_UUID,
        db_index=True,
    )
    unit = models.CharField(max_length=50)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    current_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    min_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class StockLog(models.Model):
    REASON_CHOICES = (
        ("OPENING", "Opening"),
        ("PURCHASE", "Purchase"),
        ("SALE", "Sale"),
        ("MANUAL", "Manual Closing"),
        ("ADJUSTMENT", "Adjustment"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="logs")
    change = models.DecimalField(max_digits=12, decimal_places=3)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class OpeningStock(models.Model):
    ingredient = models.OneToOneField(Ingredient, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class Vendor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=100, blank=True, null=True)
    contact_person = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)


class PurchaseInvoice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    invoice_number = models.CharField(max_length=100)

    purchased_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("vendor", "invoice_number")


class PurchaseItem(models.Model):

    invoice = models.ForeignKey(
        PurchaseInvoice,
        on_delete=models.CASCADE,
        related_name="items"
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE
    )

    quantity = models.DecimalField(max_digits=12, decimal_places=3)

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )



class ManualClosing(models.Model):
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE)
    physical_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("ingredient", "date")

class DailyStockSnapshot(models.Model):
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE)
    date = models.DateField()

    system_closing = models.DecimalField(max_digits=12, decimal_places=3)
    manual_closing = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    difference = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    class Meta:
        unique_together = ("ingredient", "date")


class DailyIngredientStock(models.Model):
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        related_name="daily_assignments",
    )
    date = models.DateField(db_index=True)
    assigned_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    consumed_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    carry_forward_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("ingredient", "date")
        ordering = ["-date", "ingredient__name"]

    @property
    def remaining_stock(self):
        remaining = self.assigned_stock - self.consumed_stock
        return remaining if remaining > 0 else Decimal("0.000")

