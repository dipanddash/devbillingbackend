import uuid
from django.db import models


class Table(models.Model):
    STATUS_CHOICES = (
        ("AVAILABLE", "Available"),
        ("OCCUPIED", "Occupied"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=10, unique=True)  # A1, A2
    floor = models.CharField(max_length=50, blank=True, null=True)
    capacity = models.PositiveIntegerField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="AVAILABLE", db_index=True)

    def __str__(self):
        return self.number
    
class TableSession(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    token_number = models.CharField(max_length=20, unique=True)

    table = models.ForeignKey(
        Table,
        on_delete=models.CASCADE,
        related_name="sessions"
    )

    customer_name = models.CharField(max_length=150)

    customer_phone = models.CharField(max_length=20)
    guest_count = models.PositiveIntegerField(default=1)

    is_active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["table", "is_active", "closed_at"]),
            models.Index(fields=["is_active", "created_at"]),
        ]

    def __str__(self):
        return f"{self.token_number} - {self.table.number}"
