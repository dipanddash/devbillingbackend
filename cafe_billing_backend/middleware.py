"""
Middleware that adds offline-awareness to every request.
Sets ``request.is_offline`` and an ``X-Offline-Mode`` response header
so the React frontend can detect the current connectivity state.
"""

from cafe_billing_backend.connectivity import is_neon_reachable


class OfflineAwareMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_offline = not is_neon_reachable(force=True)
        response = self.get_response(request)
        response["X-Offline-Mode"] = "true" if request.is_offline else "false"
        return response
