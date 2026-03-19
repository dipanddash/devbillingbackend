"""
Database router for online-only mode.

All reads/writes go to the default (Neon/PostgreSQL) database.
"""


class OfflineRouter:
    def db_for_read(self, model, **hints):
        return "default"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db in {"default", "neon", "sqlite"}

