import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_CHOICES = (
        ("ADMIN", "Admin"),
        ("STAFF", "Staff"),
        ("SNOOKER_STAFF", "Snooker Staff"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=15, choices=ROLE_CHOICES)
    phone = models.CharField(max_length=20, blank=True, null=True)
    day_locked_on = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.role = "ADMIN"
        if self.role:
            self.role = self.role.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.username} - {self.role}"

class Customer(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150)

    phone = models.CharField(max_length=20, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)


    def __str__(self):
        return f"{self.name} - {self.phone}"


class StaffSessionLog(models.Model):
    SOURCE_SYSTEM_LOGIN = "SYSTEM_LOGIN"
    SOURCE_ATTENDANCE_DESK = "ATTENDANCE_DESK"
    SOURCE_CHOICES = (
        (SOURCE_SYSTEM_LOGIN, "System Login"),
        (SOURCE_ATTENDANCE_DESK, "Attendance Desk"),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="session_logs",
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_SYSTEM_LOGIN,
    )
    login_at = models.DateTimeField(auto_now_add=True)
    logout_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-login_at"]

    def __str__(self):
        return f"{self.user.username} [{self.source}] ({self.login_at})"


class StaffReportAccess(models.Model):
    staff_user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="report_access",
        limit_choices_to={"role": "STAFF"},
    )
    allowed_reports = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["staff_user__username"]

    def __str__(self):
        return f"{self.staff_user.username} report access"
