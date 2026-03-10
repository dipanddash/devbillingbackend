from django.urls import path
from .views import (
    SnookerBoardListCreateView,
    SnookerBoardDetailView,
    ConsoleListCreateView,
    ConsoleDetailView,
    GameSessionListCreateView,
    GameSessionDetailView,
    SessionItemListCreateView,
    SessionItemDeleteView,
    CheckoutView,
    CancelSessionView,
    GamingDashboardView,
    AdminGamingAnalyticsView,
    SessionAuditLogListView,
)

urlpatterns = [
    path("dashboard/", GamingDashboardView.as_view(), name="gaming-dashboard"),
    path("admin-analytics/", AdminGamingAnalyticsView.as_view(), name="gaming-admin-analytics"),
    path("audit-logs/", SessionAuditLogListView.as_view(), name="gaming-audit-logs"),
    path("boards/", SnookerBoardListCreateView.as_view(), name="board-list-create"),
    path("boards/<uuid:pk>/", SnookerBoardDetailView.as_view(), name="board-detail"),
    path("consoles/", ConsoleListCreateView.as_view(), name="console-list-create"),
    path("consoles/<uuid:pk>/", ConsoleDetailView.as_view(), name="console-detail"),
    path("sessions/", GameSessionListCreateView.as_view(), name="session-list-create"),
    path("sessions/<uuid:pk>/", GameSessionDetailView.as_view(), name="session-detail"),
    path("sessions/<uuid:pk>/checkout/", CheckoutView.as_view(), name="session-checkout"),
    path("sessions/<uuid:pk>/cancel/", CancelSessionView.as_view(), name="session-cancel"),
    path("session-items/", SessionItemListCreateView.as_view(), name="session-item-list-create"),
    path("session-items/<uuid:pk>/", SessionItemDeleteView.as_view(), name="session-item-delete"),
]
