"""
Network connectivity checker for Neon PostgreSQL fallback.
Provides a cached check so repeated calls don't hammer the network.
"""

import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

_cache = {"online": None, "checked_at": 0}
CACHE_TTL = 30  # seconds


def is_neon_reachable(force=False):
    """
    Quick TCP socket check to the Neon PostgreSQL host.
    Results are cached for CACHE_TTL seconds to avoid repeated checks.
    """
    now = time.time()
    if (
        not force
        and _cache["online"] is not None
        and (now - _cache["checked_at"]) < CACHE_TTL
    ):
        return _cache["online"]

    host = os.getenv("DB_HOST", "")
    port = int(os.getenv("DB_PORT", "5432"))
    timeout = min(int(os.getenv("DB_CONNECT_TIMEOUT", "5")), 5)

    if not host:
        _cache.update({"online": False, "checked_at": now})
        return False

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        _cache.update({"online": True, "checked_at": now})
        logger.debug("Neon PostgreSQL is reachable.")
        return True
    except (socket.timeout, socket.error, OSError):
        _cache.update({"online": False, "checked_at": now})
        logger.info("Neon PostgreSQL is unreachable — offline mode.")
        return False
