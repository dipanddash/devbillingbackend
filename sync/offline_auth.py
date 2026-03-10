"""
Offline authentication helpers.

On a successful *online* login the caller should invoke ``cache_user_credentials``
to store the user's hashed password locally.  When the system is offline,
``authenticate_offline`` verifies the password against that cached hash.
"""

import logging

from django.contrib.auth.hashers import check_password
from django.utils import timezone

from .models import CachedCredential

logger = logging.getLogger(__name__)


def cache_user_credentials(user):
    """
    Upsert CachedCredential for *user* (called after a successful online login).
    Stores the already-hashed password from user.password — never plaintext.
    """
    CachedCredential.objects.using("sqlite").update_or_create(
        user_id=user.id,
        defaults={
            "username": user.username,
            "password_hash": user.password,  # Django's PBKDF2 hash
            "role": (getattr(user, "role", "") or "").upper(),
            "is_active": user.is_active,
        },
    )


def authenticate_offline(username, raw_password):
    """
    Verify *username* / *raw_password* against the locally cached hash.
    Returns a dict with user info on success, or None on failure.
    """
    try:
        cached = CachedCredential.objects.using("sqlite").get(
            username=username,
            is_active=True,
        )
    except CachedCredential.DoesNotExist:
        return None

    if not check_password(raw_password, cached.password_hash):
        return None

    return {
        "id": str(cached.user_id),
        "username": cached.username,
        "role": cached.role,
        "offline": True,
    }
