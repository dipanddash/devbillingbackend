"""
Database router for offline-first architecture.

- OfflineSyncQueue and CachedCredential always read/write from the 'sqlite' alias.
- All other models follow 'default' (Neon when online, SQLite when offline).
"""


class OfflineRouter:
    """Routes offline-only models to the local SQLite database."""

    SQLITE_MODELS = {"offlinesyncqueue", "cachedcredential"}

    def db_for_read(self, model, **hints):
        if model._meta.model_name in self.SQLITE_MODELS:
            return "sqlite"
        return None

    def db_for_write(self, model, **hints):
        if model._meta.model_name in self.SQLITE_MODELS:
            return "sqlite"
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Allow all models on all databases — the router only controls reads/writes.
        # This ensures `migrate --database=sqlite` creates the full schema locally.
        return None
