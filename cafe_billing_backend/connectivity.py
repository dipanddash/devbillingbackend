"""
Network connectivity checker for Neon PostgreSQL fallback.
Provides a cached database-level check so repeated calls do not hammer Neon.
"""

import logging
import time

from django.db import DatabaseError, InterfaceError, OperationalError, close_old_connections, connections

logger = logging.getLogger(__name__)

_cache = {"online": None, "checked_at": 0}
CACHE_TTL = 15  # seconds


def mark_neon_unreachable():
    _cache.update({"online": False, "checked_at": time.time()})


def _probe_neon_database():
    """
    Verify Neon with a real SQL round-trip instead of only a TCP socket probe.
    This avoids false "online" states when the socket opens but queries fail.
    """
    close_old_connections()
    connection = connections["neon"]

    # If Django is holding a psycopg connection object that's already closed,
    # reset it before opening a cursor so the probe can reconnect cleanly.
    raw_connection = getattr(connection, "connection", None)
    if raw_connection is not None and getattr(raw_connection, "closed", 0):
        connection.close()

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            return bool(row and row[0] == 1)
    except (OperationalError, DatabaseError, InterfaceError, OSError):
        connection.close()
        raise


def is_neon_reachable(force=False):
    """
    Database-level health check for Neon PostgreSQL.
    Results are cached for CACHE_TTL seconds to avoid repeated checks.
    """
    now = time.time()
    if (
        not force
        and _cache["online"] is not None
        and (now - _cache["checked_at"]) < CACHE_TTL
    ):
        return _cache["online"]

    try:
        _probe_neon_database()
        _cache.update({"online": True, "checked_at": now})
        logger.debug("Neon PostgreSQL is reachable.")
        return True
    except (OperationalError, DatabaseError, InterfaceError, OSError):
        _cache.update({"online": False, "checked_at": now})
        logger.info("Neon PostgreSQL is unreachable - offline mode.")
        return False
