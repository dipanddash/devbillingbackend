from django.contrib import admin
from .models import (
    Ingredient,
    StockLog,
    Vendor,
    PurchaseInvoice,
    PurchaseItem,
    OpeningStock,
    ManualClosing,
    DailyStockSnapshot
)

admin.site.register(Ingredient)
admin.site.register(StockLog)
admin.site.register(Vendor)
admin.site.register(PurchaseInvoice)
admin.site.register(PurchaseItem)
admin.site.register(OpeningStock)
admin.site.register(ManualClosing)
admin.site.register(DailyStockSnapshot)
