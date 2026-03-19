from rest_framework_simplejwt.authentication import JWTAuthentication


class OfflineAwareJWTAuthentication(JWTAuthentication):
    """Backward-compatible name; now behaves as standard JWT auth."""
