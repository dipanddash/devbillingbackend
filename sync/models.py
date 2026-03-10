import uuid
from django.db import models


class SyncLog(models.Model):
    """
    Tracks synced operations from offline clients to prevent duplicates.
    Each offline operation has a unique client_id (UUID) generated on the client.
    Before processing, the server checks if this client_id already exists.
    """
    client_id = models.UUIDField(unique=True, db_index=True)
    entity_type = models.CharField(max_length=50)  # order, customer
    action = models.CharField(max_length=20)  # create, update
    server_id = models.UUIDField(null=True, blank=True)  # server-generated ID
    response_data = models.JSONField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-synced_at"]

    def __str__(self):
        return f"SyncLog({self.entity_type}:{self.client_id})"


class OfflineSyncQueue(models.Model):
    """
    Local queue for operations created while offline.
    Each record is a pending create/update/delete that needs to be
    pushed to Neon PostgreSQL when connectivity returns.
    Routed to the 'sqlite' database by OfflineRouter.
    """
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("IN_PROGRESS", "In Progress"),
        ("SYNCED", "Synced"),
        ("FAILED", "Failed"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.UUIDField(unique=True, db_index=True)
    entity_type = models.CharField(max_length=50)
    action = models.CharField(max_length=20)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=5)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Queue({self.entity_type}/{self.action} — {self.status})"


class CachedCredential(models.Model):
    """
    Stores hashed credentials for previously authenticated users so they
    can log in while offline.  Password is stored as Django's standard
    PBKDF2 hash — never plain-text.
    Routed to the 'sqlite' database by OfflineRouter.
    """
    user_id = models.UUIDField(unique=True, db_index=True)
    username = models.CharField(max_length=150, unique=True)
    password_hash = models.CharField(max_length=256)
    role = models.CharField(max_length=15)
    is_active = models.BooleanField(default=True)
    cached_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cached_at"]

    def __str__(self):
        return f"CachedCredential({self.username})"
