from inventory.models import Ingredient
from django.db import models

def check_low_stock():

    low_items = Ingredient.objects.filter(
        current_stock__lte=models.F("min_stock")
    )

    alerts = []

    for item in low_items:
        alerts.append({
            "name": item.name,
            "stock": float(item.current_stock),
            "min": float(item.min_stock)
        })

    return alerts
