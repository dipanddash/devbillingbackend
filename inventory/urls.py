from django.urls import path
from .views import (
    IngredientCategoryListCreateView,
    IngredientCategoryDetailView,
    IngredientListCreateView,
    IngredientUpdateDeleteView,
    VendorListCreateView,
    VendorDetailView,
    VendorHistoryView,
    PurchaseInvoiceCreateView,
    OpeningStockInitView,
    OpeningStockStatusView,
    DailyStockSummaryView,
    DailyStockAssignView,
    ManualClosingCreateView,
    StaffManualClosingView,
    StockAuditView,
)

urlpatterns = [
    # INGREDIENT CATEGORY
    path("categories/", IngredientCategoryListCreateView.as_view()),
    path("categories/<uuid:pk>/", IngredientCategoryDetailView.as_view()),

    # INGREDIENT
    path("ingredients/", IngredientListCreateView.as_view()),
    path("ingredients/<uuid:pk>/", IngredientUpdateDeleteView.as_view()),

    # VENDOR
    path("vendors/", VendorListCreateView.as_view()),
    path("vendors/<uuid:pk>/", VendorDetailView.as_view()),
    path("vendors/<uuid:pk>/history/", VendorHistoryView.as_view()),
    path("purchase-invoices/", PurchaseInvoiceCreateView.as_view()),
    path("opening-stock/status/", OpeningStockStatusView.as_view()),
    path("opening-stock/init/", OpeningStockInitView.as_view()),
    path("daily-stock/summary/", DailyStockSummaryView.as_view()),
    path("daily-stock/assign/", DailyStockAssignView.as_view()),
    path("manual-closing/", ManualClosingCreateView.as_view()),
    path("manual-closing/me/", StaffManualClosingView.as_view()),
    path("stock-audit/", StockAuditView.as_view()),

]
