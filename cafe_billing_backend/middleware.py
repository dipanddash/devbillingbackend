"""Core middleware for database error shielding and request metadata."""

from django.db import DatabaseError, InterfaceError, OperationalError
from django.http import JsonResponse

from cafe_billing_backend.connectivity import mark_neon_unreachable


class DatabaseFailureShieldMiddleware:
    """
    Prevent raw DB connectivity tracebacks from bubbling to the client. 
    Returns a controlled JSON response for graceful frontend handling.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _is_connectivity_error(exc: Exception) -> bool:
        text = str(exc).lower()
        connectivity_markers = (
            "could not connect",
            "connection refused",
            "connection timed out",
            "server closed the connection",
            "terminating connection",
            "network is unreachable",
            "temporarily unavailable",
            "connection to server",
            "ssl error",
        )
        return any(marker in text for marker in connectivity_markers)

    def __call__(self, request):
        try:
            return self.get_response(request)
        except (OperationalError, InterfaceError) as exc:
            mark_neon_unreachable()
            return JsonResponse(
                {"detail": "Database unavailable", "code": "DB_OFFLINE"},
                status=503,
            )
        except DatabaseError as exc:
            if self._is_connectivity_error(exc):
                mark_neon_unreachable()
                return JsonResponse(
                    {"detail": "Database unavailable", "code": "DB_OFFLINE"},
                    status=503,
                )
            raise


class OfflineAwareMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_offline = False
        response = self.get_response(request)
        response["X-Offline-Mode"] = "false"
        return response
