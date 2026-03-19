"""
Online-only connectivity helpers.

Offline mode has been removed from the project, so these helpers now
consistently report online availability.
"""


def mark_neon_unreachable():
    """Kept for backward compatibility; no-op in online-only mode."""
    return None


def is_neon_reachable(force=False):
    """Always true in online-only mode."""
    return True

