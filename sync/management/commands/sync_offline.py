"""
Management command: sync_offline

Pushes pending offline records from local SQLite to Neon PostgreSQL
in small batches.  Safe to run repeatedly (idempotent via SyncLog).

Usage:
    python manage.py sync_offline              # one batch
    python manage.py sync_offline --all        # drain the queue
    python manage.py sync_offline --batch 20   # custom batch size
"""

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Sync pending offline records from SQLite to Neon PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=10,
            help="Number of records per batch (default: 10).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Keep processing until the queue is empty.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=2,
            help="Seconds to wait between batches when using --all (default: 2).",
        )

    def handle(self, *args, **options):
        from sync.sync_service import sync_pending_records

        batch_size = options["batch"]
        drain = options["all"]
        interval = options["interval"]

        total_synced = 0
        total_failed = 0

        while True:
            result = sync_pending_records(batch_size=batch_size)
            status = result["status"]
            total_synced += result.get("synced", 0)
            total_failed += result.get("failed", 0)

            self.stdout.write(
                f"  batch: synced={result.get('synced', 0)}  "
                f"failed={result.get('failed', 0)}  "
                f"remaining={result.get('remaining', 0)}  "
                f"status={status}"
            )

            if status == "offline":
                self.stderr.write(self.style.WARNING("Neon is unreachable — aborting."))
                break

            if not drain or result.get("remaining", 0) == 0:
                break

            time.sleep(interval)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done.  total_synced={total_synced}  total_failed={total_failed}"
            )
        )
