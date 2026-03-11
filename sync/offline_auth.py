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


def _upsert_local_user(user):
    """
    Ensure a mirror user exists in the local sqlite DB so JWT auth works in OFFLINE mode.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    defaults = {
        "username": user.username,
        "password": user.password,  # already hashed
        "role": (getattr(user, "role", "") or "").upper() or "ADMIN",
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }
    User.objects.using("sqlite").update_or_create(id=user.id, defaults=defaults)

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
    _upsert_local_user(user)


def authenticate_offline(username, raw_password):
    """
    Verify *username* / *raw_password* against the locally cached hash.
    Returns a dict with user info on success, or None on failure.
    """
    cached = None
    try:
        cached = CachedCredential.objects.using("sqlite").get(
            username=username,
            is_active=True,
        )
    except CachedCredential.DoesNotExist:
        cached = None

    if cached and not check_password(raw_password, cached.password_hash):
        return None

    # Fallback: validate against local sqlite User if cached credential is missing.
    user_obj = None
    if cached is None:
        try:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            user_obj = User.objects.using("sqlite").get(username=username, is_active=True)
        except Exception:
            user_obj = None

        if not user_obj or not check_password(raw_password, user_obj.password):
            return None

        # Cache for next time.
        try:
            CachedCredential.objects.using("sqlite").update_or_create(
                user_id=user_obj.id,
                defaults={
                    "username": user_obj.username,
                    "password_hash": user_obj.password,
                    "role": (getattr(user_obj, "role", "") or "").upper(),
                    "is_active": user_obj.is_active,
                },
            )
        except Exception:
            pass

    try:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if cached:
            User.objects.using("sqlite").update_or_create(
                id=cached.user_id,
                defaults={
                    "username": cached.username,
                    "password": cached.password_hash,
                    "role": (cached.role or "").upper() or "ADMIN",
                    "is_active": cached.is_active,
                    "is_staff": True,
                },
            )
    except Exception:
        # Don't block offline login if local user sync fails
        pass

    return {
        "id": str(cached.user_id if cached else user_obj.id),
        "username": (cached.username if cached else user_obj.username),
        "role": (cached.role if cached else getattr(user_obj, "role", "")).upper(),
        "offline": True,
    }
