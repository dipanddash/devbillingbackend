from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count, Avg, F
from django.db.models.functions import ExtractHour

from accounts.permissions import IsAdminRole, IsAdminOrSnookerStaff
from .models import (
    SnookerBoard, Console, GameSession, SessionItem,
    SessionPayment, SessionAuditLog,
)
from .serializers import (
    SnookerBoardSerializer, ConsoleSerializer,
    GameSessionListSerializer, GameSessionCreateSerializer,
    SessionItemSerializer, SessionPaymentSerializer,
    SessionAuditLogSerializer, CheckoutSerializer,
)


# ── Resource management (admin only) ──

class SnookerBoardListCreateView(generics.ListCreateAPIView):
    queryset = SnookerBoard.objects.all()
    serializer_class = SnookerBoardSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAdminOrSnookerStaff()]


class SnookerBoardDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = SnookerBoard.objects.all()
    serializer_class = SnookerBoardSerializer
    permission_classes = [IsAdminRole]


class ConsoleListCreateView(generics.ListCreateAPIView):
    queryset = Console.objects.all()
    serializer_class = ConsoleSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAdminOrSnookerStaff()]


class ConsoleDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Console.objects.all()
    serializer_class = ConsoleSerializer
    permission_classes = [IsAdminRole]


# ── Game Sessions ──

class GameSessionListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminOrSnookerStaff]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return GameSessionCreateSerializer
        return GameSessionListSerializer

    def get_queryset(self):
        qs = GameSession.objects.select_related("console", "staff").prefetch_related("boards", "items", "payments")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        service = self.request.query_params.get("service_type")
        if service:
            qs = qs.filter(service_type=service.upper())
        today_only = self.request.query_params.get("today")
        if today_only == "true":
            qs = qs.filter(check_in__date=timezone.localdate())
        return qs

    def perform_create(self, serializer):
        serializer.save(staff=self.request.user)


class GameSessionDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAdminOrSnookerStaff]
    queryset = GameSession.objects.select_related("console", "staff").prefetch_related("boards", "items", "payments")

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return GameSessionCreateSerializer
        return GameSessionListSerializer


# ── Session Items (food add-ons) ──

class SessionItemListCreateView(generics.ListCreateAPIView):
    serializer_class = SessionItemSerializer
    permission_classes = [IsAdminOrSnookerStaff]

    def get_queryset(self):
        session_id = self.request.query_params.get("session")
        if session_id:
            return SessionItem.objects.filter(session_id=session_id)
        return SessionItem.objects.none()


class SessionItemDeleteView(generics.DestroyAPIView):
    serializer_class = SessionItemSerializer
    permission_classes = [IsAdminOrSnookerStaff]
    queryset = SessionItem.objects.all()


# ── Checkout ──

class CheckoutView(APIView):
    permission_classes = [IsAdminOrSnookerStaff]

    def post(self, request, pk):
        try:
            session = GameSession.objects.select_related("console", "staff").prefetch_related("boards", "items").get(pk=pk)
        except GameSession.DoesNotExist:
            return Response({"detail": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

        if session.status != "ACTIVE":
            return Response({"detail": "Session is not active."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        now = timezone.now()
        hours = (now - session.check_in).total_seconds() / 3600

        # Calculate service cost
        if session.service_type == "SNOOKER":
            board_count = session.boards.count() or 1
            service_cost = round(float(session.price_per_board_per_hour) * board_count * hours, 2)
        else:
            service_cost = round(float(session.price_per_person_per_hour) * session.num_players * hours, 2)

        food_total = float(session.items.aggregate(total=Sum("total_price"))["total"] or 0)
        discount = float(data.get("discount_amount", 0))
        calculated_total = round(service_cost + food_total - discount, 2)

        submitted_final = data.get("final_amount")
        final_amount = float(submitted_final) if submitted_final is not None else calculated_total

        with transaction.atomic():
            # Audit log if amount was manually edited
            if submitted_final is not None and round(final_amount, 2) != round(calculated_total, 2):
                SessionAuditLog.objects.create(
                    session=session,
                    field_changed="final_amount",
                    old_value=str(calculated_total),
                    new_value=str(final_amount),
                    reason=data.get("reason", ""),
                    changed_by=request.user,
                )

            session.check_out = now
            session.discount_amount = discount
            session.final_amount = final_amount
            session.status = "COMPLETED"
            session.save()

            # Create payments
            for payment_data in data.get("payments", []):
                SessionPayment.objects.create(
                    session=session,
                    method=payment_data["method"],
                    amount=payment_data["amount"],
                    reference_id=payment_data.get("reference_id", ""),
                )

        result = GameSessionListSerializer(session).data
        result["service_cost"] = service_cost
        result["food_total"] = food_total
        result["calculated_total"] = calculated_total
        return Response(result, status=status.HTTP_200_OK)


# ── Cancel ──

class CancelSessionView(APIView):
    permission_classes = [IsAdminOrSnookerStaff]

    def post(self, request, pk):
        try:
            session = GameSession.objects.get(pk=pk)
        except GameSession.DoesNotExist:
            return Response({"detail": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

        if session.status != "ACTIVE":
            return Response({"detail": "Session is not active."}, status=status.HTTP_400_BAD_REQUEST)

        session.status = "CANCELLED"
        session.check_out = timezone.now()
        session.save()
        return Response({"detail": "Session cancelled."}, status=status.HTTP_200_OK)


# ── Dashboard Stats ──

class GamingDashboardView(APIView):
    permission_classes = [IsAdminOrSnookerStaff]

    def get(self, request):
        today = timezone.localdate()
        today_sessions = GameSession.objects.filter(check_in__date=today)
        active_sessions = GameSession.objects.filter(status="ACTIVE")

        completed_today = today_sessions.filter(status="COMPLETED")

        snooker_revenue = float(
            completed_today.filter(service_type="SNOOKER").aggregate(t=Sum("final_amount"))["t"] or 0
        )
        console_revenue = float(
            completed_today.filter(service_type="CONSOLE").aggregate(t=Sum("final_amount"))["t"] or 0
        )

        food_revenue = float(
            SessionItem.objects.filter(
                session__in=completed_today
            ).aggregate(t=Sum("total_price"))["t"] or 0
        )

        total_boards = SnookerBoard.objects.filter(is_active=True).count()
        occupied_boards = SnookerBoard.objects.filter(
            is_active=True, sessions__status="ACTIVE"
        ).distinct().count()

        total_consoles = Console.objects.filter(is_active=True).count()
        occupied_consoles = Console.objects.filter(
            is_active=True, sessions__status="ACTIVE"
        ).distinct().count()

        return Response({
            "customers_today": today_sessions.values("customer_phone").distinct().count(),
            "active_sessions": active_sessions.count(),
            "completed_today": completed_today.count(),
            "snooker_revenue": snooker_revenue,
            "console_revenue": console_revenue,
            "food_revenue": food_revenue,
            "total_revenue": round(snooker_revenue + console_revenue, 2),
            "available_boards": total_boards - occupied_boards,
            "occupied_boards": occupied_boards,
            "total_boards": total_boards,
            "available_consoles": total_consoles - occupied_consoles,
            "occupied_consoles": occupied_consoles,
            "total_consoles": total_consoles,
        })


# ── Admin Analytics ──

class AdminGamingAnalyticsView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        today = timezone.localdate()
        today_sessions = GameSession.objects.filter(check_in__date=today)
        active_sessions = GameSession.objects.filter(status="ACTIVE")
        completed_today = today_sessions.filter(status="COMPLETED")

        snooker_revenue = float(
            completed_today.filter(service_type="SNOOKER").aggregate(t=Sum("final_amount"))["t"] or 0
        )
        console_revenue = float(
            completed_today.filter(service_type="CONSOLE").aggregate(t=Sum("final_amount"))["t"] or 0
        )
        food_revenue = float(
            SessionItem.objects.filter(session__in=completed_today).aggregate(t=Sum("total_price"))["t"] or 0
        )

        total_boards = SnookerBoard.objects.filter(is_active=True).count()
        occupied_boards = SnookerBoard.objects.filter(is_active=True, sessions__status="ACTIVE").distinct().count()
        total_consoles = Console.objects.filter(is_active=True).count()
        occupied_consoles = Console.objects.filter(is_active=True, sessions__status="ACTIVE").distinct().count()

        # Average session duration (completed today)
        durations = []
        for s in completed_today.filter(check_out__isnull=False):
            durations.append((s.check_out - s.check_in).total_seconds() / 60)
        avg_duration = round(sum(durations) / len(durations), 1) if durations else 0

        # Peak hours (by check-in hour)
        peak_hours = list(
            today_sessions.annotate(hour=ExtractHour("check_in"))
            .values("hour")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )

        # Board utilization
        board_sessions = {}
        for b in SnookerBoard.objects.filter(is_active=True):
            count = b.sessions.filter(check_in__date=today).count()
            board_sessions[f"Board {b.number}"] = count

        # Console utilization
        console_sessions = {}
        for c in Console.objects.filter(is_active=True):
            count = c.sessions.filter(check_in__date=today).count()
            console_sessions[c.name] = count

        # Top selling food items
        top_items = list(
            SessionItem.objects.filter(session__check_in__date=today)
            .values("item_name")
            .annotate(total_qty=Sum("quantity"), total_amount=Sum("total_price"))
            .order_by("-total_qty")[:10]
        )

        # Staff-wise stats
        staff_stats = list(
            today_sessions.filter(status="COMPLETED")
            .values("staff__username")
            .annotate(
                sessions_count=Count("id"),
                revenue=Sum("final_amount"),
            )
            .order_by("-revenue")
        )

        # Override / audit log count
        override_count = SessionAuditLog.objects.filter(
            session__check_in__date=today
        ).count()

        return Response({
            "customers_today": today_sessions.values("customer_phone").distinct().count(),
            "active_sessions": active_sessions.count(),
            "completed_today": completed_today.count(),
            "cancelled_today": today_sessions.filter(status="CANCELLED").count(),
            "snooker_revenue": snooker_revenue,
            "console_revenue": console_revenue,
            "food_revenue": food_revenue,
            "total_revenue": round(snooker_revenue + console_revenue, 2),
            "available_boards": total_boards - occupied_boards,
            "occupied_boards": occupied_boards,
            "total_boards": total_boards,
            "available_consoles": total_consoles - occupied_consoles,
            "occupied_consoles": occupied_consoles,
            "total_consoles": total_consoles,
            "avg_duration_minutes": avg_duration,
            "peak_hours": peak_hours,
            "board_utilization": board_sessions,
            "console_utilization": console_sessions,
            "top_selling_items": top_items,
            "staff_stats": staff_stats,
            "override_count": override_count,
        })


# ── Audit Logs ──

class SessionAuditLogListView(generics.ListAPIView):
    serializer_class = SessionAuditLogSerializer
    permission_classes = [IsAdminRole]

    def get_queryset(self):
        qs = SessionAuditLog.objects.select_related("session", "changed_by").all()
        today_only = self.request.query_params.get("today")
        if today_only == "true":
            qs = qs.filter(session__check_in__date=timezone.localdate())
        return qs
