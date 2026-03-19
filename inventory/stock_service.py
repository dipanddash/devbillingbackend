from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import DailyIngredientStock, Ingredient, StockLog


def _to_decimal(value, default: str = "0.000") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _quantize_amount(value: Decimal, places: str = "0.000") -> Decimal:
    return value.quantize(Decimal(places))


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _previous_day_remaining(ingredient_id, target_date, db_alias: str) -> Decimal:
    previous = (
        DailyIngredientStock.objects.using(db_alias)
        .filter(ingredient_id=ingredient_id, date__lt=target_date)
        .order_by("-date")
        .first()
    )
    if not previous:
        return Decimal("0.000")
    remaining = previous.assigned_stock - previous.consumed_stock
    return remaining if remaining > 0 else Decimal("0.000")


def upsert_daily_assignment(
    *,
    items: Iterable[dict],
    target_date,
    user,
    db_alias: str,
):
    rows = list(items or [])
    if not rows:
        raise ValidationError("items must be a non-empty list")

    ingredient_ids = [str(row.get("ingredient") or "").strip() for row in rows if isinstance(row, dict)]
    if not ingredient_ids:
        raise ValidationError("At least one ingredient is required for assignment")

    updated_rows = []
    seen = set()
    with transaction.atomic(using=db_alias):
        ingredients = {
            str(ingredient.id): ingredient
            for ingredient in Ingredient.objects.using(db_alias)
            .select_for_update()
            .filter(id__in=ingredient_ids)
        }
        existing_assignments = {
            str(row.ingredient_id): row
            for row in DailyIngredientStock.objects.using(db_alias)
            .select_for_update()
            .filter(date=target_date, ingredient_id__in=ingredient_ids)
        }

        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError("Each item must be an object")

            ingredient_id = str(row.get("ingredient") or "").strip()
            if not ingredient_id:
                raise ValidationError("ingredient is required for each row")
            if ingredient_id in seen:
                raise ValidationError(f"Duplicate ingredient in payload: {ingredient_id}")
            seen.add(ingredient_id)

            ingredient = ingredients.get(ingredient_id)
            if ingredient is None:
                raise ValidationError(f"Invalid ingredient id: {ingredient_id}")

            assigned_stock = _to_decimal(row.get("quantity"))
            if assigned_stock < 0:
                raise ValidationError(f"Assigned stock cannot be negative for {ingredient.name}")

            if assigned_stock > _to_decimal(ingredient.current_stock):
                raise ValidationError(
                    (
                        f"Assigned stock for {ingredient.name} cannot exceed total stock "
                        f"({ingredient.current_stock}). Please enter {ingredient.current_stock} or less."
                    )
                )

            assignment = existing_assignments.get(ingredient_id)
            already_used = _to_decimal(assignment.consumed_stock if assignment else 0)
            if assigned_stock < already_used:
                raise ValidationError(
                    f"Assigned stock for {ingredient.name} cannot be below used quantity ({already_used})"
                )

            carry_forward = (
                _to_decimal(assignment.carry_forward_stock)
                if assignment
                else _previous_day_remaining(ingredient.id, target_date, db_alias)
            )
            if assignment is None:
                assignment = DailyIngredientStock(
                    ingredient=ingredient,
                    date=target_date,
                    consumed_stock=Decimal("0.000"),
                )

            assignment.assigned_stock = _quantize_amount(assigned_stock)
            assignment.carry_forward_stock = _quantize_amount(carry_forward)
            assignment.updated_by = user if getattr(user, "is_authenticated", False) else None
            assignment.save(using=db_alias)
            updated_rows.append(assignment)

    return updated_rows


def _normalize_usage(ingredient_usage: Dict) -> Dict[str, Decimal]:
    normalized = {}
    for ingredient_id, raw_qty in (ingredient_usage or {}).items():
        qty = _to_decimal(raw_qty)
        if qty <= 0:
            continue
        normalized[str(ingredient_id)] = normalized.get(str(ingredient_id), Decimal("0.000")) + qty
    return normalized


def consume_ingredients_for_sale(
    *,
    ingredient_usage: Dict,
    db_alias: str,
    user=None,
    operation_date=None,
):
    usage = _normalize_usage(ingredient_usage)
    if not usage:
        return

    target_date = operation_date or timezone.localdate()
    ingredient_ids = list(usage.keys())

    with transaction.atomic(using=db_alias):
        ingredients = {
            str(ingredient.id): ingredient
            for ingredient in Ingredient.objects.using(db_alias)
            .select_for_update()
            .filter(id__in=ingredient_ids)
        }
        assignments = {
            str(row.ingredient_id): row
            for row in DailyIngredientStock.objects.using(db_alias)
            .select_for_update()
            .filter(date=target_date, ingredient_id__in=ingredient_ids)
        }

        updated_ingredients = []
        updated_assignments = []
        stock_logs = []

        for ingredient_id, used_qty in usage.items():
            ingredient = ingredients.get(ingredient_id)
            if ingredient is None:
                raise ValidationError("Ingredient not found for stock deduction")

            assignment = assignments.get(ingredient_id)
            if assignment is None or assignment.assigned_stock <= 0:
                raise ValidationError(
                    f"Opening stock is not available for {ingredient.name}. Please contact admin."
                )

            remaining_today = assignment.assigned_stock - assignment.consumed_stock
            if remaining_today < used_qty:
                raise ValidationError(
                    f"Assigned stock for today is exhausted for {ingredient.name}. Please contact admin."
                )

            if _to_decimal(ingredient.current_stock) < used_qty:
                raise ValidationError(
                    f"Total stock is insufficient for {ingredient.name}. Please contact admin."
                )

            ingredient.current_stock = _quantize_amount(_to_decimal(ingredient.current_stock) - used_qty)
            assignment.consumed_stock = _quantize_amount(_to_decimal(assignment.consumed_stock) + used_qty)
            assignment.updated_by = user if getattr(user, "is_authenticated", False) else None
            assignment.updated_at = timezone.now()

            updated_ingredients.append(ingredient)
            updated_assignments.append(assignment)
            stock_logs.append(
                StockLog(
                    ingredient=ingredient,
                    change=-used_qty,
                    reason="SALE",
                    user=user if getattr(user, "is_authenticated", False) else None,
                )
            )

        if updated_ingredients:
            Ingredient.objects.using(db_alias).bulk_update(updated_ingredients, ["current_stock"])
        if updated_assignments:
            DailyIngredientStock.objects.using(db_alias).bulk_update(
                updated_assignments,
                ["consumed_stock", "updated_by", "updated_at"],
            )
        if stock_logs:
            StockLog.objects.using(db_alias).bulk_create(stock_logs)


def reverse_consumed_ingredients(
    *,
    ingredient_usage: Dict,
    db_alias: str,
    user=None,
    operation_date=None,
):
    usage = _normalize_usage(ingredient_usage)
    if not usage:
        return

    target_date = operation_date or timezone.localdate()
    ingredient_ids = list(usage.keys())

    with transaction.atomic(using=db_alias):
        ingredients = {
            str(ingredient.id): ingredient
            for ingredient in Ingredient.objects.using(db_alias)
            .select_for_update()
            .filter(id__in=ingredient_ids)
        }
        assignments = {
            str(row.ingredient_id): row
            for row in DailyIngredientStock.objects.using(db_alias)
            .select_for_update()
            .filter(date=target_date, ingredient_id__in=ingredient_ids)
        }

        updated_ingredients = []
        updated_assignments = []
        stock_logs = []

        for ingredient_id, restore_qty in usage.items():
            ingredient = ingredients.get(ingredient_id)
            if ingredient is None:
                raise ValidationError("Ingredient not found for stock reversal")

            assignment = assignments.get(ingredient_id)
            if assignment:
                next_consumed = _to_decimal(assignment.consumed_stock) - restore_qty
                assignment.consumed_stock = _quantize_amount(
                    next_consumed if next_consumed > 0 else Decimal("0.000")
                )
                assignment.updated_by = user if getattr(user, "is_authenticated", False) else None
                assignment.updated_at = timezone.now()
                updated_assignments.append(assignment)

            ingredient.current_stock = _quantize_amount(_to_decimal(ingredient.current_stock) + restore_qty)
            updated_ingredients.append(ingredient)
            stock_logs.append(
                StockLog(
                    ingredient=ingredient,
                    change=restore_qty,
                    reason="ADJUSTMENT",
                    user=user if getattr(user, "is_authenticated", False) else None,
                )
            )

        if updated_ingredients:
            Ingredient.objects.using(db_alias).bulk_update(updated_ingredients, ["current_stock"])
        if updated_assignments:
            DailyIngredientStock.objects.using(db_alias).bulk_update(
                updated_assignments,
                ["consumed_stock", "updated_by", "updated_at"],
            )
        if stock_logs:
            StockLog.objects.using(db_alias).bulk_create(stock_logs)


def build_daily_summary(*, target_date, db_alias: str):
    ingredients = list(
        Ingredient.objects.using(db_alias).select_related("category").all().order_by("name")
    )
    ingredient_ids = [ingredient.id for ingredient in ingredients]
    today_rows = {
        str(row.ingredient_id): row
        for row in DailyIngredientStock.objects.using(db_alias)
        .filter(date=target_date, ingredient_id__in=ingredient_ids)
    }

    rows = []
    total_assigned = Decimal("0.000")
    total_used = Decimal("0.000")
    total_remaining = Decimal("0.000")
    total_valuation = Decimal("0.00")

    for ingredient in ingredients:
        today = today_rows.get(str(ingredient.id))
        carry_forward = (
            _to_decimal(today.carry_forward_stock)
            if today
            else _previous_day_remaining(ingredient.id, target_date, db_alias)
        )
        assigned = _to_decimal(today.assigned_stock if today else 0)
        used = _to_decimal(today.consumed_stock if today else 0)
        remaining_today = assigned - used
        if remaining_today < 0:
            remaining_today = Decimal("0.000")

        total_stock = _to_decimal(ingredient.current_stock)
        min_stock = _to_decimal(ingredient.min_stock)
        unit_price = _to_decimal(ingredient.unit_price, default="0.00")
        valuation = _quantize_money(total_stock * unit_price)

        if total_stock <= 0:
            health = "out"
        elif total_stock <= min_stock:
            health = "low"
        else:
            health = "good"

        rows.append(
            {
                "ingredient_id": str(ingredient.id),
                "ingredient_name": ingredient.name,
                "category_id": str(ingredient.category_id) if ingredient.category_id else None,
                "category_name": ingredient.category.name if ingredient.category else "OTHERS",
                "unit": ingredient.unit,
                "unit_price": unit_price,
                "assigned_today": _quantize_amount(assigned),
                "used_today": _quantize_amount(used),
                "remaining_today": _quantize_amount(remaining_today),
                "carry_forward": _quantize_amount(carry_forward),
                "total_stock": _quantize_amount(total_stock),
                "min_stock": _quantize_amount(min_stock),
                "valuation": valuation,
                "health": health,
                "is_active": ingredient.is_active,
            }
        )

        total_assigned += assigned
        total_used += used
        total_remaining += remaining_today
        total_valuation += valuation

    return {
        "date": target_date.isoformat(),
        "totals": {
            "ingredients_count": len(rows),
            "assigned_today": _quantize_amount(total_assigned),
            "used_today": _quantize_amount(total_used),
            "remaining_today": _quantize_amount(total_remaining),
            "valuation": _quantize_money(total_valuation),
        },
        "rows": rows,
    }
