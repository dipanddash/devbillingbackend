import uuid
from django.db import models
from django.conf import settings

class AssetCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Asset(models.Model):

    STATUS_CHOICES = (
        ("WORKING", "Working"),
        ("DAMAGED", "Damaged"),
        ("REPAIR", "Under Repair"),
        ("LOST", "Lost"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150)

    category = models.ForeignKey(
        AssetCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    quantity = models.PositiveIntegerField(default=1)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="WORKING"
    )

    purchase_date = models.DateField(null=True, blank=True)

    warranty_end = models.DateField(null=True, blank=True)

    remarks = models.TextField(blank=True, null=True)

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class AssetLog(models.Model):

    ACTION_CHOICES = (
        ("ADD", "Added"),
        ("UPDATE", "Updated"),
        ("DAMAGE", "Damaged"),
        ("REPAIR", "Repair"),
        ("LOST", "Lost"),
        ("DISPOSE", "Disposed"),
    )

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="logs")

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)

    quantity_change = models.IntegerField(default=0)

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True
    )

    note = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
