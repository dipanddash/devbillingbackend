from django.contrib.auth import get_user_model
from django.db import OperationalError
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.settings import api_settings


class OfflineAwareJWTAuthentication(JWTAuthentication):
    """
    Resolve user from sqlite when Neon is unreachable, instead of raising 500.
    """

    def get_user(self, validated_token):
        user_id = validated_token.get(api_settings.USER_ID_CLAIM)
        if user_id is None:
            raise AuthenticationFailed("Token contained no recognizable user identification")

        user_model = get_user_model()
        try:
            return super().get_user(validated_token)
        except (OperationalError, user_model.DoesNotExist):
            user = user_model.objects.using("sqlite").filter(
                **{api_settings.USER_ID_FIELD: user_id}
            ).first()
            if user is None:
                raise AuthenticationFailed("User not found", code="user_not_found")
            if not user.is_active:
                raise AuthenticationFailed("User is inactive", code="user_inactive")
            return user
