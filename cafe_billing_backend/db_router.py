"""
Database router for offline-first architecture.

- OfflineSyncQueue and CachedCredential always read/write from the 'sqlite' alias.
- All other models route dynamically:
  - SQLite when internet/Neon is unreachable
  - Neon when internet/Neon is reachable
"""

import os

from cafe_billing_backend.connectivity import is_neon_reachable


class OfflineRouter:
    """Routes offline-only models to the local SQLite database."""

    SQLITE_MODELS = {"offlinesyncqueue", "cachedcredential"}

    @staticmethod
    def _is_forced_offline():
        return os.getenv("FORCE_OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes"}

    @classmethod
    def _is_offline(cls):
        if cls._is_forced_offline():
            return True
        # Use cached connectivity here so repeated ORM routing does not churn the
        # Neon connection during a single request cycle.
        return not is_neon_reachable(force=False)

    def db_for_read(self, model, **hints):
        if model._meta.model_name in self.SQLITE_MODELS:
            return "sqlite"
        return "sqlite" if self._is_offline() else "neon"

    def db_for_write(self, model, **hints):
        if model._meta.model_name in self.SQLITE_MODELS:
            return "sqlite"
        return "sqlite" if self._is_offline() else "neon"

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Allow all models on all databases — the router only controls reads/writes.
        # This ensures `migrate --database=sqlite` creates the full schema locally.
        return None
