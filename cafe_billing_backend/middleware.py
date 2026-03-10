"""
Middleware that adds offline-awareness to every request.
Sets ``request.is_offline`` and an ``X-Offline-Mode`` response header
so the React frontend can detect the current connectivity state.
"""

import logging

logger = logging.getLogger(__name__)


class OfflineAwareMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        request.is_offline = getattr(settings, "OFFLINE_MODE", False)
        response = self.get_response(request)
        response["X-Offline-Mode"] = "true" if request.is_offline else "false"
        return response
