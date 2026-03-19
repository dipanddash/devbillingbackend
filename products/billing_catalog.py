import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from orders.billing import quantize_money, to_decimal
from inventory.models import DailyIngredientStock
from products.models import Combo, Product


CATALOG_SCHEMA_VERSION = "2026-03-18.1"


def _serialize_catalog_signature(items):
    signature_rows = []
    for row in items:
        signature_rows.append(
            {
                "entity": row.get("entity"),
                "id": row.get("id"),
                "base_price": row.get("base_price"),
                "gst_percent": row.get("gst_percent"),
                "unit_total": row.get("unit_total"),
                "recipe_blueprint": row.get("recipe_blueprint", []),
                "is_active": row.get("is_active"),
                "is_available": row.get("is_available"),
            }
        )
    signature_rows.sort(key=lambda x: (x.get("entity", ""), x.get("id", "")))
    return json.dumps(signature_rows, sort_keys=True, separators=(",", ":"))


def _build_product_catalog_rows(products_qs, daily_remaining_map):
    rows = []
    for product in products_qs:
        gst_percent = to_decimal(product.gst_percent, default="0.00")
        amounts = {
            "base_price": quantize_money(product.price),
            "gst_percent": gst_percent,
            "unit_tax": quantize_money((quantize_money(product.price) * gst_percent) / Decimal("100")),
        }
        amounts["unit_total"] = quantize_money(amounts["base_price"] + amounts["unit_tax"])

        recipe_blueprint = []
        is_available = True
        availability_reason = ""
        recipes = list(product.recipes.all())
        if not recipes:
            is_available = False
            availability_reason = "Recipe not configured"
        else:
            for recipe in recipes:
                ingredient = recipe.ingredient
                recipe_blueprint.append(
                    {
                        "ingredient_id": str(recipe.ingredient_id),
                        "ingredient_name": ingredient.name if ingredient else "",
                        "quantity": str(recipe.quantity),
                        "unit": ingredient.unit if ingredient else "",
                    }
                )
                if ingredient is None:
                    is_available = False
                    availability_reason = "Recipe ingredient missing"
                elif is_available and ingredient.current_stock < recipe.quantity:
                    is_available = False
                    availability_reason = f"Insufficient stock: {ingredient.name}"
                elif is_available:
                    remaining_today = daily_remaining_map.get(str(recipe.ingredient_id))
                    if remaining_today is None:
                        is_available = False
                        availability_reason = "Opening stock is not available. Please contact admin."
                    elif remaining_today < recipe.quantity:
                        is_available = False
                        availability_reason = f"Assigned stock exhausted: {ingredient.name}"

        rows.append(
            {
                "entity": "product",
                "id": str(product.id),
                "name": product.name,
                "category": {
                    "id": str(product.category_id) if product.category_id else None,
                    "name": product.category.name if product.category else "",
                },
                "base_price": str(amounts["base_price"]),
                "gst_percent": str(gst_percent),
                "tax_mode": "EXCLUSIVE",
                "unit_tax": str(amounts["unit_tax"]),
                "unit_total": str(amounts["unit_total"]),
                "recipe_blueprint": recipe_blueprint,
                "recipe_summary": {
                    "ingredients_count": len(recipe_blueprint),
                    "has_recipe": bool(recipe_blueprint),
                },
                "is_active": bool(product.is_active),
                "is_available": bool(is_available),
                "availability_reason": availability_reason,
            }
        )
    return rows


def _build_combo_catalog_rows(combos_qs, daily_remaining_map):
    rows = []
    for combo in combos_qs:
        gst_percent = to_decimal(combo.gst_percent, default="0.00")
        amounts = {
            "base_price": quantize_money(combo.price),
            "gst_percent": gst_percent,
            "unit_tax": quantize_money((quantize_money(combo.price) * gst_percent) / Decimal("100")),
        }
        amounts["unit_total"] = quantize_money(amounts["base_price"] + amounts["unit_tax"])

        combo_items = []
        ingredient_blueprint = defaultdict(Decimal)
        ingredient_meta = {}
        is_available = True
        availability_reason = ""

        items = list(combo.items.all())
        if not items:
            is_available = False
            availability_reason = "Combo has no products"
        else:
            for combo_item in items:
                combo_items.append(
                    {
                        "product_id": str(combo_item.product_id),
                        "product_name": combo_item.product.name if combo_item.product else "",
                        "quantity": int(combo_item.quantity),
                    }
                )
                recipes = list(combo_item.product.recipes.all()) if combo_item.product_id else []
                if not recipes and is_available:
                    is_available = False
                    availability_reason = (
                        f"Recipe not configured: {combo_item.product.name}"
                        if combo_item.product
                        else "Combo product missing"
                    )
                for recipe in recipes:
                    ingredient = recipe.ingredient
                    ingredient_blueprint[recipe.ingredient_id] += recipe.quantity * combo_item.quantity
                    ingredient_meta[recipe.ingredient_id] = ingredient
                    if ingredient is None and is_available:
                        is_available = False
                        availability_reason = "Recipe ingredient missing"

        recipe_blueprint = []
        for ingredient_id, qty in ingredient_blueprint.items():
            ingredient = ingredient_meta.get(ingredient_id)
            recipe_blueprint.append(
                {
                    "ingredient_id": str(ingredient_id),
                    "ingredient_name": ingredient.name if ingredient else "",
                    "quantity": str(qty),
                    "unit": ingredient.unit if ingredient else "",
                }
            )
            if ingredient and is_available and ingredient.current_stock < qty:
                is_available = False
                availability_reason = f"Insufficient stock: {ingredient.name}"
            elif ingredient and is_available:
                remaining_today = daily_remaining_map.get(str(ingredient_id))
                if remaining_today is None:
                    is_available = False
                    availability_reason = "Opening stock is not available. Please contact admin."
                elif remaining_today < qty:
                    is_available = False
                    availability_reason = f"Assigned stock exhausted: {ingredient.name}"

        rows.append(
            {
                "entity": "combo",
                "id": str(combo.id),
                "name": combo.name,
                "category": {"id": None, "name": "COMBO"},
                "base_price": str(amounts["base_price"]),
                "gst_percent": str(gst_percent),
                "tax_mode": "EXCLUSIVE",
                "unit_tax": str(amounts["unit_tax"]),
                "unit_total": str(amounts["unit_total"]),
                "combo_items": combo_items,
                "recipe_blueprint": recipe_blueprint,
                "recipe_summary": {
                    "ingredients_count": len(recipe_blueprint),
                    "has_recipe": bool(recipe_blueprint),
                },
                "is_active": bool(combo.is_active),
                "is_available": bool(is_available),
                "availability_reason": availability_reason,
            }
        )
    return rows


def build_billing_catalog_payload(db_alias="neon"):
    generated_at = timezone.now().isoformat()
    target_date = timezone.localdate()

    products_qs = (
        Product.objects.using(db_alias)
        .filter(is_active=True)
        .select_related("category")
        .prefetch_related("recipes__ingredient")
    )
    combos_qs = (
        Combo.objects.using(db_alias)
        .filter(is_active=True)
        .prefetch_related("items__product__recipes__ingredient")
    )

    ingredient_ids = set()
    for product in products_qs:
        for recipe in product.recipes.all():
            ingredient_ids.add(str(recipe.ingredient_id))
    for combo in combos_qs:
        for combo_item in combo.items.all():
            if not combo_item.product:
                continue
            for recipe in combo_item.product.recipes.all():
                ingredient_ids.add(str(recipe.ingredient_id))

    daily_rows = {
        str(row.ingredient_id): max(Decimal("0.000"), row.assigned_stock - row.consumed_stock)
        for row in DailyIngredientStock.objects.using(db_alias)
        .filter(date=target_date, ingredient_id__in=ingredient_ids)
    }

    items = _build_product_catalog_rows(products_qs, daily_rows) + _build_combo_catalog_rows(combos_qs, daily_rows)
    signature_source = _serialize_catalog_signature(items)
    version_hash = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:16]

    return {
        "meta": {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_version": version_hash,
            "generated_at": generated_at,
            "source_db": db_alias,
            "items_count": len(items),
        },
        "items": items,
    }
