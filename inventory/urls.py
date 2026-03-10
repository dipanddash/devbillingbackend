from django.urls import path
from .views import (
    IngredientListCreateView,
    IngredientDetailView,
    IngredientUpdateDeleteView,
    VendorListCreateView,
    VendorDetailView,
    VendorHistoryView,
    PurchaseInvoiceCreateView,
    OpeningStockInitView,
    OpeningStockStatusView,
    ManualClosingCreateView,
    StaffManualClosingView,
    StockAuditView,
)

urlpatterns = [

    # INGREDIENT
    path("ingredients/", IngredientListCreateView.as_view()),
    path("ingredients/<uuid:pk>/", IngredientDetailView.as_view()),
    path("ingredients/<uuid:pk>/", IngredientUpdateDeleteView.as_view()),

    # VENDOR
    path("vendors/", VendorListCreateView.as_view()),
    path("vendors/<uuid:pk>/", VendorDetailView.as_view()),
    path("vendors/<uuid:pk>/history/", VendorHistoryView.as_view()),
    path("purchase-invoices/", PurchaseInvoiceCreateView.as_view()),
    path("opening-stock/status/", OpeningStockStatusView.as_view()),
    path("opening-stock/init/", OpeningStockInitView.as_view()),
    path("manual-closing/", ManualClosingCreateView.as_view()),
    path("manual-closing/me/", StaffManualClosingView.as_view()),
    path("stock-audit/", StockAuditView.as_view()),

]
