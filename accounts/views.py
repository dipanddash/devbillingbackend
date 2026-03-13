import uuid
from uuid import uuid4
from rest_framework.views import APIView
from rest_framework import generics
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.contrib.auth import authenticate
from django.utils import timezone
from datetime import datetime
from decimal import Decimal
from django.db.models import Count, DecimalField, Max, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db import transaction
from .serializers import (
    LoginSerializer,
    CustomerSerializer,
    CustomTokenObtainPairSerializer,
    CustomTokenRefreshSerializer,
    StaffUserSerializer,
    AdminUserSerializer,
    MeProfileSerializer,
    StaffReportAccessSerializer,
    SnookerStaffUserSerializer,
)
from .permissions import IsAdminRole
from .models import Customer, StaffReportAccess, StaffSessionLog, User
from inventory.models import DailyStockSnapshot, ManualClosing
from cafe_billing_backend.connectivity import is_neon_reachable

class LoginView(APIView):
    def post(self, request):
        # Offline mode: authenticate against locally cached credentials
        if not is_neon_reachable(force=True):
            from sync.offline_auth import authenticate_offline

            username = str(request.data.get("username", "")).strip()
            password = str(request.data.get("password", ""))
            result = authenticate_offline(username, password)
            if result is None:
                return Response(
                    {"detail": "Invalid credentials (offline mode)."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            return Response(result, status=status.HTTP_200_OK)

        # Online mode: normal Django authentication
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        resolved_role = "ADMIN" if user.is_superuser else (getattr(user, "role", "") or "").upper()

        return Response({
            "id": user.id,
            "username": user.username,
            "role": resolved_role
        }, status=status.HTTP_200_OK)


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        # Offline mode: issue a simple token from cached credentials
        if not is_neon_reachable(force=True):
            from sync.offline_auth import authenticate_offline
            from rest_framework_simplejwt.tokens import RefreshToken

            username = str(request.data.get("username", "")).strip()
            password = str(request.data.get("password", ""))
            result = authenticate_offline(username, password)
            if result is None:
                return Response(
                    {"detail": "Invalid credentials (offline mode)."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Build a minimal JWT without hitting the database
            refresh = RefreshToken()
            refresh["user_id"] = result["id"]
            refresh["username"] = result["username"]
            refresh["role"] = result["role"]

            return Response({
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "id": result["id"],
                "username": result["username"],
                "role": result["role"],
            })

        # Online mode: normal JWT flow
        return super().post(request, *args, **kwargs)


class CustomTokenRefreshView(TokenRefreshView):
    serializer_class = CustomTokenRefreshSerializer


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if (getattr(request.user, "role", "") or "").upper() in ("STAFF", "SNOOKER_STAFF"):
            StaffSessionLog.objects.filter(
                user=request.user,
                logout_at__isnull=True
            ).update(logout_at=timezone.now())

        return Response({"message": "Logged out"}, status=status.HTTP_200_OK)


class MeProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = MeProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = MeProfileSerializer(
            request.user,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class MePermissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = "ADMIN" if request.user.is_superuser else (getattr(request.user, "role", "") or "").upper()
        if request.user.is_superuser or role == "ADMIN":
            capabilities = [
                "Manage users and permissions",
                "Access financial reports",
                "Configure system settings",
            ]
            modules = [
                "Dashboard",
                "Invoices",
                "Products",
                "Customers",
                "Payments",
                "Reports",
                "Inventory",
                "Settings",
                "Staff Management",
                "Gaming Sessions",
            ]
        elif role == "SNOOKER_STAFF":
            capabilities = [
                "Manage gaming sessions",
                "Check-in and checkout customers",
                "Add food items to sessions",
            ]
            modules = [
                "Dashboard",
                "New Session",
                "Active Sessions",
            ]
        else:
            capabilities = [
                "Manage assigned orders",
                "Use POS and kitchen operations",
                "Access limited operational reports",
            ]
            modules = [
                "Dashboard",
                "POS",
                "Tables",
                "Kitchen",
                "Orders",
            ]

        return Response(
            {
                "role": role,
                "capabilities": capabilities,
                "modules": modules,
            },
            status=status.HTTP_200_OK,
        )


class AttendanceDeskListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        date_param = request.GET.get("date")
        target_date = timezone.localdate()
        if date_param:
            try:
                target_date = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return Response({"error": "date must be YYYY-MM-DD"}, status=400)

        logs = (
            StaffSessionLog.objects
            .filter(
                user__role="STAFF",
                source=StaffSessionLog.SOURCE_ATTENDANCE_DESK,
                login_at__date=target_date,
            )
            .select_related("user")
            .order_by("-login_at")
        )

        return Response(
            [
                {
                    "id": log.id,
                    "staff": log.user.username,
                    "source": log.source,
                    "login_at_iso": timezone.localtime(log.login_at).isoformat(),
                    "logout_at_iso": timezone.localtime(log.logout_at).isoformat() if log.logout_at else None,
                }
                for log in logs
            ],
            status=200,
        )


class AttendanceDeskCheckInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        username = str(request.data.get("username", "")).strip()
        password = str(request.data.get("password", "")).strip()
        if not username or not password:
            return Response({"error": "username and password are required"}, status=400)

        user = authenticate(username=username, password=password)
        if not user:
            return Response({"error": "Invalid username or password"}, status=400)
        if user.role != "STAFF":
            return Response({"error": "Only STAFF users can check in here"}, status=400)
        if not user.is_active:
            today = timezone.localdate()
            if user.day_locked_on and user.day_locked_on < today:
                user.is_active = True
                user.day_locked_on = None
                user.save(update_fields=["is_active", "day_locked_on"])
            elif user.day_locked_on == today:
                return Response({"error": "Can't login for the day. Ask admin to activate your account."}, status=400)
            else:
                return Response({"error": "Staff user is inactive"}, status=400)

        open_log = (
            StaffSessionLog.objects
            .filter(
                user=user,
                source=StaffSessionLog.SOURCE_ATTENDANCE_DESK,
                logout_at__isnull=True,
            )
            .order_by("-login_at")
            .first()
        )

        if open_log:
            open_log.logout_at = timezone.now()
            open_log.save(update_fields=["logout_at"])
            return Response(
                {
                    "id": open_log.id,
                    "staff": user.username,
                    "source": open_log.source,
                    "login_at_iso": timezone.localtime(open_log.login_at).isoformat(),
                    "logout_at_iso": timezone.localtime(open_log.logout_at).isoformat() if open_log.logout_at else None,
                    "action": "CHECK_OUT",
                },
                status=200,
            )

        log = StaffSessionLog.objects.create(
            user=user,
            source=StaffSessionLog.SOURCE_ATTENDANCE_DESK,
        )
        return Response(
            {
                "id": log.id,
                "staff": user.username,
                "source": log.source,
                "login_at_iso": timezone.localtime(log.login_at).isoformat(),
                "logout_at_iso": None,
                "action": "CHECK_IN",
            },
            status=201,
        )


class AttendanceDeskCheckOutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        log_id = request.data.get("id")
        if log_id in (None, ""):
            return Response({"error": "id is required"}, status=400)

        try:
            log = StaffSessionLog.objects.select_related("user").get(
                id=log_id,
                user__role="STAFF",
                source=StaffSessionLog.SOURCE_ATTENDANCE_DESK,
            )
        except StaffSessionLog.DoesNotExist:
            return Response({"error": "Attendance session not found"}, status=404)

        if not log.logout_at:
            log.logout_at = timezone.now()
            log.save(update_fields=["logout_at"])

        return Response(
            {
                "id": log.id,
                "staff": log.user.username,
                "source": log.source,
                "login_at_iso": timezone.localtime(log.login_at).isoformat(),
                "logout_at_iso": timezone.localtime(log.logout_at).isoformat() if log.logout_at else None,
            },
            status=200,
        )


class StaffReportAccessAdminView(APIView):
    permission_classes = [IsAdminRole]

    def _get_staff_user(self, request):
        staff_id = request.query_params.get("staff_id")
        if not staff_id:
            return None, Response({"error": "staff_id is required"}, status=400)
        try:
            staff_uuid = uuid.UUID(str(staff_id))
        except (ValueError, TypeError):
            return None, Response({"error": "Invalid staff_id"}, status=400)
        try:
            user = User.objects.get(id=staff_uuid, role="STAFF")
            return user, None
        except User.DoesNotExist:
            return None, Response({"error": "Staff user not found"}, status=404)

    def get(self, request):
        staff_user, error_response = self._get_staff_user(request)
        if error_response:
            return error_response
        access, _ = StaffReportAccess.objects.get_or_create(
            staff_user=staff_user,
            defaults={"allowed_reports": []},
        )
        return Response(StaffReportAccessSerializer(access).data, status=200)

    def patch(self, request):
        staff_user, error_response = self._get_staff_user(request)
        if error_response:
            return error_response
        access, _ = StaffReportAccess.objects.get_or_create(
            staff_user=staff_user,
            defaults={"allowed_reports": []},
        )
        serializer = StaffReportAccessSerializer(access, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=200)


class MyStaffReportAccessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if (getattr(request.user, "role", "") or "").upper() != "STAFF":
            return Response(
                {
                    "staff_user_id": str(request.user.id),
                    "username": request.user.username,
                    "allowed_reports": [],
                },
                status=200,
            )
        access, _ = StaffReportAccess.objects.get_or_create(
            staff_user=request.user,
            defaults={"allowed_reports": []},
        )
        return Response(StaffReportAccessSerializer(access).data, status=200)

class CustomerView(APIView):

    permission_classes = [IsAdminRole]

    # GET → List customers
    def get(self, request):

        paid_non_cancelled_filter = Q(order__payment_status="PAID") & ~Q(order__status="CANCELLED")

        customers = (
            Customer.objects
            .annotate(
                order_count=Count("order", filter=paid_non_cancelled_filter, distinct=True),
                visit_count=Count(
                    "order__session",
                    filter=paid_non_cancelled_filter & Q(order__session__isnull=False),
                    distinct=True,
                ),
                total_spent=Coalesce(
                    Sum("order__total_amount", filter=paid_non_cancelled_filter),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                last_visit_at=Max("order__created_at", filter=paid_non_cancelled_filter),
            )
            .order_by("-created_at")
        )

        serializer = CustomerSerializer(customers, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

    # POST → Create customer
    def post(self, request):

        serializer = CustomerSerializer(data=request.data)

        if serializer.is_valid():

            serializer.save()

            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class StaffUserListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = StaffUserSerializer

    def get_queryset(self):
        return User.objects.filter(role__in=["STAFF", "SNOOKER_STAFF"]).order_by("username")

    def perform_create(self, serializer):
        staff_user = serializer.save()

        if getattr(self.request, "is_offline", False) or not is_neon_reachable(force=False):
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="staff",
                action="create",
                payload={
                    "id": str(staff_user.id),
                    "username": staff_user.username,
                    "first_name": staff_user.first_name,
                    "last_name": staff_user.last_name,
                    "email": staff_user.email,
                    "phone": staff_user.phone,
                    "role": (staff_user.role or "").upper(),
                    "is_active": bool(staff_user.is_active),
                    "password_hash": staff_user.password,
                    "is_staff": bool(staff_user.is_staff),
                    "is_superuser": bool(staff_user.is_superuser),
                },
            )


class StaffUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = StaffUserSerializer
    queryset = User.objects.filter(role__in=["STAFF", "SNOOKER_STAFF"])

    def perform_destroy(self, instance):
        if instance.id == self.request.user.id:
            raise ValidationError("You cannot delete your own account.")
        instance.delete()


class StaffUserStatusView(APIView):
    permission_classes = [IsAdminRole]

    def patch(self, request, pk):
        try:
            staff_user = User.objects.get(pk=pk, role__in=["STAFF", "SNOOKER_STAFF"])
        except User.DoesNotExist:
            return Response({"error": "Staff user not found"}, status=404)

        if staff_user.id == request.user.id:
            return Response(
                {"error": "You cannot change your own active status"},
                status=400
            )

        is_active = request.data.get("is_active")
        if not isinstance(is_active, bool):
            return Response(
                {"error": "is_active must be true or false"},
                status=400
            )

        if is_active:
            staff_user.is_active = True
            staff_user.day_locked_on = None
            staff_user.save(update_fields=["is_active", "day_locked_on"])
        else:
            staff_user.is_active = False
            staff_user.day_locked_on = None
            staff_user.save(update_fields=["is_active", "day_locked_on"])

        reset_rows = 0
        if is_active:
            today = timezone.localdate()
            with transaction.atomic():
                rows_qs = ManualClosing.objects.filter(entered_by=staff_user, date=today)
                ingredient_ids = list(rows_qs.values_list("ingredient_id", flat=True))
                reset_rows = rows_qs.update(physical_quantity=0)
                if ingredient_ids:
                    for snapshot in DailyStockSnapshot.objects.filter(date=today, ingredient_id__in=ingredient_ids):
                        snapshot.manual_closing = 0
                        snapshot.difference = snapshot.system_closing
                        snapshot.save(update_fields=["manual_closing", "difference"])

        return Response(
            {
                "id": str(staff_user.id),
                "username": staff_user.username,
                "is_active": staff_user.is_active,
                "manual_closing_reset_rows": reset_rows,
            },
            status=200
        )


class AdminUserListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AdminUserSerializer

    def get_queryset(self):
        return User.objects.filter(role="ADMIN").order_by("username")

    def perform_create(self, serializer):
        serializer.save(role="ADMIN")


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AdminUserSerializer
    queryset = User.objects.filter(role="ADMIN")

    def perform_destroy(self, instance):
        if instance.id == self.request.user.id:
            raise ValidationError("You cannot delete your own account.")
        instance.delete()


class SnookerStaffUserListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = SnookerStaffUserSerializer

    def get_queryset(self):
        return User.objects.filter(role="SNOOKER_STAFF").order_by("username")

    def perform_create(self, serializer):
        serializer.save(role="SNOOKER_STAFF")


class SnookerStaffUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = SnookerStaffUserSerializer
    queryset = User.objects.filter(role="SNOOKER_STAFF")

    def perform_destroy(self, instance):
        if instance.id == self.request.user.id:
            raise ValidationError("You cannot delete your own account.")
        instance.delete()


class SystemResetView(APIView):
    """
    Superuser-only endpoint that deletes all application data
    except the requesting superuser account.

    Uses a dynamic model-discovery approach so that any new
    business app / model added in the future is automatically
    included in the cleanup — no manual update needed.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only superusers can perform a system reset."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from accounts.services import perform_system_reset

        try:
            neon_online = is_neon_reachable(force=True)
            if neon_online:
                perform_system_reset(superuser_id=request.user.id, using="neon")
            perform_system_reset(superuser_id=request.user.id, using="sqlite")
        except Exception as e:
            return Response(
                {"detail": f"System reset failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "detail": "System reset complete. All data except your superuser account has been deleted.",
                "online_reset": neon_online,
                "offline_reset": True,
            },
            status=status.HTTP_200_OK,
        )
