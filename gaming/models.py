import uuid
from django.db import models
from django.conf import settings


class SnookerBoard(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.PositiveIntegerField(unique=True)
    label = models.CharField(max_length=50, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"Board {self.number}"


class Console(models.Model):
    CONSOLE_TYPES = (
        ("PS2", "PlayStation 2"),
        ("PS4", "PlayStation 4"),
        ("PS5", "PlayStation 5"),
        ("XBOX", "Xbox"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    console_type = models.CharField(max_length=10, choices=CONSOLE_TYPES)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["console_type", "name"]

    def __str__(self):
        return f"{self.name} ({self.console_type})"


class GameSession(models.Model):
    SERVICE_TYPES = (
        ("SNOOKER", "Snooker"),
        ("CONSOLE", "Console"),
    )

    STATUS_CHOICES = (
        ("ACTIVE", "Active"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    customer_name = models.CharField(max_length=150)
    customer_phone = models.CharField(max_length=20)
    customer_email = models.EmailField(blank=True, default="")

    service_type = models.CharField(max_length=10, choices=SERVICE_TYPES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE", db_index=True)

    # Snooker-specific
    boards = models.ManyToManyField(SnookerBoard, blank=True, related_name="sessions")
    price_per_board_per_hour = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Console-specific
    console = models.ForeignKey(Console, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions")
    console_type = models.CharField(max_length=10, blank=True, default="")
    price_per_person_per_hour = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    num_players = models.PositiveIntegerField(default=1)

    check_in = models.DateTimeField(auto_now_add=True, db_index=True)
    check_out = models.DateTimeField(null=True, blank=True)

    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="gaming_sessions",
    )

    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-check_in"]
        indexes = [
            models.Index(fields=["status", "check_in"]),
            models.Index(fields=["service_type", "status"]),
        ]

    def __str__(self):
        return f"{self.service_type} - {self.customer_name} ({self.status})"


class SessionItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="items")

    product = models.ForeignKey(
        "products.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    item_name = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.item_name} x{self.quantity}"


class SessionPayment(models.Model):
    METHOD_CHOICES = (
        ("CASH", "Cash"),
        ("UPI", "UPI"),
        ("CARD", "Card"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference_id = models.CharField(max_length=100, blank=True, default="")
    paid_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.method} - {self.amount}"


class SessionAuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="audit_logs")
    field_changed = models.CharField(max_length=100)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    reason = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.field_changed} changed on {self.session}"
