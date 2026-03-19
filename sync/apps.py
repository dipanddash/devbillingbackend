import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sync"

    def ready(self):
        # Skip during migrate / makemigrations to avoid recursion
        if len(sys.argv) > 1 and sys.argv[1] in (
            "migrate",
            "makemigrations",
            "collectstatic",
            "createsuperuser",
            "shell",
        ):
            return
        self._ensure_sqlite_ready()

    @staticmethod
    def _ensure_sqlite_ready():
        """Auto-migrate the local SQLite database if it doesn't exist yet."""
        from django.conf import settings

        db_conf = settings.DATABASES.get("sqlite")
        if not db_conf:
            return

        db_path = str(db_conf.get("NAME", ""))
        if not db_path:
            return

        needs_setup = not os.path.exists(db_path) or os.path.getsize(db_path) < 100
        if needs_setup:
            try:
                from django.core.management import call_command

                logger.info("Setting up local SQLite database...")
                call_command(
                    "migrate",
                    "--database=sqlite",
                    "--no-input",
                    verbosity=0,
                )
                logger.info("Local SQLite database ready.")
            except Exception as exc:
                logger.warning(
                    "Auto-migrate sqlite failed: %s. "
                    'Run "python manage.py prepare_offline" manually.',
                    exc,
                )
