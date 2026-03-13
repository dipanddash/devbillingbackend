"""
Management command: prepare_offline

Migrates the local SQLite database and caches all active user
credentials so the application can function fully when offline.

Usage:
    python manage.py prepare_offline
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Prepare the local SQLite database for offline operation."

    def handle(self, *args, **options):
        self._migrate_sqlite()
        self._cache_credentials()
        self._refresh_local_mirror()
        self.stdout.write(self.style.SUCCESS("Offline database is ready."))

    def _migrate_sqlite(self):
        from django.core.management import call_command

        self.stdout.write("Migrating local SQLite database …")
        call_command("migrate", "--database=sqlite", "--no-input", verbosity=1)

    def _cache_credentials(self):
        from accounts.models import User
        from sync.offline_auth import cache_user_credentials

        users = User.objects.using("neon").filter(is_active=True)
        count = 0
        for user in users:
            try:
                cache_user_credentials(user)
                count += 1
            except Exception as exc:
                self.stderr.write(f"  Could not cache {user.username}: {exc}")

        self.stdout.write(f"Cached credentials for {count} active user(s).")

    def _refresh_local_mirror(self):
        from sync.sync_service import refresh_sqlite_from_neon

        self.stdout.write("Refreshing local SQLite mirror from Neon...")
        result = refresh_sqlite_from_neon()
        status = result.get("status")
        if status == "offline":
            self.stderr.write("Neon is unreachable, skipped mirror refresh.")
            return

        refreshed = result.get("refreshed", {})
        summary = ", ".join(f"{name}={count}" for name, count in refreshed.items())
        if status == "ok":
            self.stdout.write(f"Mirrored data: {summary}")
            return

        failed = result.get("failed", {})
        self.stdout.write(f"Mirrored data (partial): {summary}")
        if failed:
            self.stderr.write("Mirror failed for: " + ", ".join(failed.keys()))
