import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sync"

    def ready(self):
        # Offline sqlite bootstrap removed (online-only mode).
        logger.debug("Sync app ready (online-only mode).")
