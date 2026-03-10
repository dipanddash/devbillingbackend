from django.urls import path
from .views import *

urlpatterns = [
    path("sales/daily/", ReportByKeyView.as_view(), {"report_key": "daily-sales"}),
    path("sales/product/", ReportByKeyView.as_view(), {"report_key": "product-wise-sales"}),
    path("payments/method/", ReportByKeyView.as_view(), {"report_key": "payment-method"}),
    path("purchase/daily/", ReportByKeyView.as_view(), {"report_key": "purchase"}),
    path("stock/consumption/", ReportByKeyView.as_view(), {"report_key": "stock-consumption"}),
    path("gst/", ReportByKeyView.as_view(), {"report_key": "gst"}),
    path("wastage/", ReportByKeyView.as_view(), {"report_key": "wastage"}),
    path("stock/low/", ReportByKeyView.as_view(), {"report_key": "low-stock"}),
    path("sales/peak-time/", ReportByKeyView.as_view(), {"report_key": "peak-sales-time"}),
    path("staff/login-logout/", ReportByKeyView.as_view(), {"report_key": "staff-attendance"}),
    path("discount/abuse/", ReportByKeyView.as_view(), {"report_key": "discount"}),
    path("orders/cancelled/", ReportByKeyView.as_view(), {"report_key": "cancelled-void"}),
    path("dashboard/", DashboardSummaryView.as_view()),
    path("combo/performance/", ReportByKeyView.as_view(), {"report_key": "combo"}),
    path("coupons/usage/", CouponUsageReportView.as_view()),

    # New unified report endpoints
    path("v2/<slug:report_key>/", ReportByKeyView.as_view()),
    path("v2/v2/<slug:report_key>/", ReportByKeyView.as_view()),

]
