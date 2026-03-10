from rest_framework.permissions import BasePermission


def _role(user):
    return (getattr(user, "role", "") or "").upper()


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and (request.user.is_superuser or _role(request.user) == "ADMIN")
        )


class IsStaffRole(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and _role(request.user) == "STAFF"
        )


class IsAdminOrStaff(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and (
                request.user.is_superuser
                or _role(request.user) in ["ADMIN", "STAFF"]
            )
        )


class IsSnookerStaff(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and _role(request.user) == "SNOOKER_STAFF"
        )


class IsAdminOrSnookerStaff(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and (
                request.user.is_superuser
                or _role(request.user) in ["ADMIN", "SNOOKER_STAFF"]
            )
        )
