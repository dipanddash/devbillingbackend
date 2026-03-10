from django.urls import path
from .views import AddOrderItemsView, OrderCreateView, OrderInvoiceView, OrderStatusUpdateView, TodayOrderListView, OrderListView, RecentOrderListView, send_whatsapp
from .views import OrderPaymentView
from .views import OrderDetailView
from .views import OrderCancelView
from .views import CouponListCreateView, CouponRetrieveUpdateDeleteView, CouponValidateView, CouponUsageListView
urlpatterns = [

    path("create/", OrderCreateView.as_view()),
    path("today/", TodayOrderListView.as_view()),
    path("recent/", RecentOrderListView.as_view()),
    path("status/<uuid:pk>/", OrderStatusUpdateView.as_view()),
    path("cancel/<uuid:pk>/", OrderCancelView.as_view()),
    path("pay/<uuid:pk>/", OrderPaymentView.as_view()),
    path("invoice/<uuid:pk>/", OrderInvoiceView.as_view()),
    path(
    "add-items/<uuid:order_id>/",
    AddOrderItemsView.as_view()
),

    path("send-whatsapp/", send_whatsapp, name="send_whatsapp"),
    path("coupons/", CouponListCreateView.as_view()),
    path("coupons/<int:pk>/", CouponRetrieveUpdateDeleteView.as_view()),
    path("coupons/validate/", CouponValidateView.as_view()),
    path("coupons/usage/", CouponUsageListView.as_view()),
    path("list/", OrderListView.as_view()),
    path("<uuid:pk>/", OrderDetailView.as_view()),
]
