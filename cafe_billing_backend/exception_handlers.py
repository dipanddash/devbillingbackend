from django.db import DatabaseError, InterfaceError, OperationalError
from rest_framework.response import Response
from rest_framework.views import exception_handler


def drf_exception_handler(exc, context):
    if isinstance(exc, (OperationalError, InterfaceError, DatabaseError)):
        return Response(
            {"detail": "Database unavailable", "code": "DB_OFFLINE"},
            status=503,
        )
    return exception_handler(exc, context)

