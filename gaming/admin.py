from django.contrib import admin
from .models import (
    SnookerBoard, Console, GameSession, SessionItem,
    SessionPayment, SessionAuditLog,
)

admin.site.register(SnookerBoard)
admin.site.register(Console)
admin.site.register(GameSession)
admin.site.register(SessionItem)
admin.site.register(SessionPayment)
admin.site.register(SessionAuditLog)
