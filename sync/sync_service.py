"""
Background sync service.

- Push pending offline records from local SQLite to Neon PostgreSQL.
- Mirror selected Neon data back into local SQLite so the app can keep
  working with the latest data while offline.
"""

import logging
import threading
import time
from decimal import Decimal, InvalidOperation

from django.db import DatabaseError, OperationalError, close_old_connections, connections, models, transaction
from django.db.utils import IntegrityError, InterfaceError
from django.utils import timezone
from django.utils.dateparse import parse_date

from cafe_billing_backend.connectivity import is_neon_reachable
from orders.billing import (
    calculate_line_amounts,
    calculate_payable_amount,
    normalize_phone,
    parse_positive_quantity,
    quantize_money,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
TRIGGER_COOLDOWN_SECONDS = 10
MIRROR_BATCH_SIZE = 250
MIRROR_RETRY_ATTEMPTS = 2

_sync_trigger_lock = threading.Lock()
_mirror_refresh_lock = threading.Lock()
_sqlite_schema_lock = threading.Lock()
_sync_in_progress = False
_last_triggered_at = 0.0
_sqlite_schema_checked = False


# ── helpers ──────────────────────────────────────────────────────────

def _safe_decimal(value, default="0.00"):
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else Decimal(default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _safe_int(value, default=1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _quantize_money(value):
    return _safe_decimal(value).quantize(Decimal("0.01"))


def _ensure_sqlite_schema_ready():
    """
    Apply pending sqlite migrations once per process before mirror writes.
    This prevents missing-column errors when local schema lags code.
    """
    global _sqlite_schema_checked

    if _sqlite_schema_checked:
        return

    with _sqlite_schema_lock:
        if _sqlite_schema_checked:
            return
        try:
            from django.core.management import call_command

            call_command("migrate", "--database=sqlite", "--no-input", verbosity=0)
            _sqlite_schema_checked = True
        except Exception as exc:
            logger.warning("Could not apply sqlite migrations before mirror refresh: %s", exc)


def _build_recipe_map(product_ids):
    from products.models import Recipe

    recipes_by_product = {}
    if not product_ids:
        return recipes_by_product

    rows = (
        Recipe.objects.using("neon")
        .filter(product_id__in=product_ids)
        .select_related("ingredient", "product")
    )
    for row in rows:
        recipes_by_product.setdefault(row.product_id, []).append(row)
    return recipes_by_product


def _ingredient_usage_from_payload(items_data):
    usage = {}
    for item_data in items_data or []:
        qty = _safe_decimal(item_data.get("quantity"), default="1")
        if qty <= 0:
            qty = Decimal("1")

        for blueprint in item_data.get("ingredient_blueprint", []) or []:
            ingredient_id = blueprint.get("ingredient_id")
            if not ingredient_id:
                continue
            per_unit = _safe_decimal(blueprint.get("quantity"), default="0.000")
            if per_unit <= 0:
                continue
            usage[ingredient_id] = usage.get(ingredient_id, Decimal("0")) + (per_unit * qty)
    return usage


def _accumulate_addon_ingredient_usage(order_item, ingredient_usage):
    addon_counts = {}
    addons_by_id = {}
    addon_rows = order_item.addons.using("neon").all()
    for addon_row in addon_rows:
        addon = addon_row.addon
        if not addon or not addon.ingredient_id:
            continue
        addon_key = str(addon.id)
        addon_counts[addon_key] = addon_counts.get(addon_key, 0) + 1
        addons_by_id[addon_key] = addon

    if not addon_counts:
        return

    item_qty = _safe_decimal(getattr(order_item, "quantity", 0), default="0")
    if item_qty <= 0:
        return

    for addon_key, qty_per_item in addon_counts.items():
        addon = addons_by_id.get(addon_key)
        if addon is None:
            continue
        per_unit_qty = _safe_decimal(getattr(addon, "ingredient_quantity", 0), default="0.000")
        if per_unit_qty <= 0:
            continue
        ingredient_usage[addon.ingredient_id] = ingredient_usage.get(addon.ingredient_id, Decimal("0")) + (
            per_unit_qty * _safe_decimal(qty_per_item, default="0") * item_qty
        )


def _deduct_ingredients_for_paid_order(order, items_data=None):
    from inventory.stock_service import consume_ingredients_for_sale

    ingredient_usage = _ingredient_usage_from_payload(items_data)

    if not ingredient_usage:
        order_items = list(
            order.items.using("neon")
            .select_related("product", "combo")
            .prefetch_related("combo__items", "addons__addon__ingredient")
        )
        if not order_items:
            return

        product_ids = set()
        for item in order_items:
            if item.product_id:
                product_ids.add(item.product_id)
                continue
            if item.combo_id and item.combo:
                for combo_item in item.combo.items.using("neon").all():
                    product_ids.add(combo_item.product_id)

        recipes_by_product = _build_recipe_map(product_ids)
        for item in order_items:
            if item.product_id:
                item_recipes = recipes_by_product.get(item.product_id, [])
                if not item_recipes:
                    raise ValueError(f"No recipe configured for product {item.product_id}")
                for recipe in item_recipes:
                    ingredient_usage[recipe.ingredient_id] = ingredient_usage.get(recipe.ingredient_id, Decimal("0")) + (
                        recipe.quantity * item.quantity
                    )
                continue

            if not item.combo_id or not item.combo:
                continue
            for combo_product in item.combo.items.using("neon").all():
                combo_recipes = recipes_by_product.get(combo_product.product_id, [])
                if not combo_recipes:
                    raise ValueError(f"No recipe configured for combo product {combo_product.product_id}")
                combined_qty = combo_product.quantity * item.quantity
                for recipe in combo_recipes:
                    ingredient_usage[recipe.ingredient_id] = ingredient_usage.get(recipe.ingredient_id, Decimal("0")) + (
                        recipe.quantity * combined_qty
                    )

            _accumulate_addon_ingredient_usage(item, ingredient_usage)

    consume_ingredients_for_sale(
        ingredient_usage=ingredient_usage,
        db_alias="neon",
        user=order.staff if order.staff_id else None,
        operation_date=timezone.localdate(),
    )


# ── public entry point ───────────────────────────────────────────────

def sync_pending_records(batch_size=BATCH_SIZE):
    """
    Process up to *batch_size* PENDING items from the offline queue.
    Returns a summary dict: {'status', 'synced', 'failed', 'remaining'}.
    """
    from .models import OfflineSyncQueue

    if not is_neon_reachable(force=True):
        return {"status": "offline", "synced": 0, "failed": 0, "remaining": 0}

    pending = list(
        OfflineSyncQueue.objects.using("sqlite")
        .filter(status="PENDING", retry_count__lt=models.F("max_retries"))
        .order_by("created_at")[:batch_size]
    )

    if not pending:
        return {"status": "idle", "synced": 0, "failed": 0, "remaining": 0}

    synced = 0
    failed = 0

    for item in pending:
        item.status = "IN_PROGRESS"
        item.save(using="sqlite", update_fields=["status", "updated_at"])
        try:
            _process_item(item)
            item.status = "SYNCED"
            item.synced_at = timezone.now()
            item.save(using="sqlite", update_fields=["status", "synced_at", "updated_at"])
            synced += 1
        except Exception as exc:
            logger.exception("Sync failed for %s", item.client_id)
            item.retry_count += 1
            item.error_message = str(exc)[:500]
            item.status = "FAILED" if item.retry_count >= item.max_retries else "PENDING"
            item.save(
                using="sqlite",
                update_fields=["status", "retry_count", "error_message", "updated_at"],
            )
            failed += 1

    remaining = (
        OfflineSyncQueue.objects.using("sqlite")
        .filter(status="PENDING", retry_count__lt=models.F("max_retries"))
        .count()
    )

    return {"status": "ok", "synced": synced, "failed": failed, "remaining": remaining}


def refresh_sqlite_from_neon():
    """
    Pull core business data from Neon into local SQLite.
    This keeps reads available offline even for data created while online.
    """
    _ensure_sqlite_schema_ready()

    if not _mirror_refresh_lock.acquire(blocking=False):
        return {"status": "busy", "refreshed": {}, "failed": {}}

    if not is_neon_reachable(force=True):
        _mirror_refresh_lock.release()
        return {"status": "offline", "refreshed": {}}

    from accounts.models import Customer, StaffReportAccess, StaffSessionLog, User
    from orders.models import Order, OrderItem, OrderItemAddon
    from payments.models import Payment
    from inventory.models import (
        Ingredient,
        IngredientCategory,
    )
    from products.models import Addon, Category, Combo, ComboItem, Product, Recipe
    from tables.models import Table, TableSession

    refreshed = {}
    failed = {}

    close_old_connections()
    try:
        # Pull parent tables first, then dependents.
        # Querysets are factories so retries always start with a fresh neon cursor state.
        jobs = [
            ("users", User, lambda: User.objects.using("neon").all(), [
                "username", "password", "first_name", "last_name", "email",
                "is_staff", "is_active", "is_superuser", "last_login",
                "date_joined", "role", "phone", "day_locked_on",
            ], "id"),
            ("customers", Customer, lambda: Customer.objects.using("neon").all(), ["name", "phone", "created_at"], "id"),
            ("categories", Category, lambda: Category.objects.using("neon").all(), ["name", "image"], "id"),
            ("products", Product, lambda: Product.objects.using("neon").select_related("category"), ["name", "category_id", "price", "gst_percent", "image", "is_active"], "id"),
            ("ingredient_categories", IngredientCategory, lambda: IngredientCategory.objects.using("neon").all(), ["name", "is_active", "created_at", "updated_at"], "id"),
            ("ingredients", Ingredient, lambda: Ingredient.objects.using("neon").select_related("category"), ["name", "category_id", "unit", "unit_price", "current_stock", "min_stock", "is_active"], "id"),
            ("recipes", Recipe, lambda: Recipe.objects.using("neon").all(), ["id", "product_id", "ingredient_id", "quantity"], "id"),
            ("addons", Addon, lambda: Addon.objects.using("neon").all(), ["name", "price", "image", "ingredient_id", "ingredient_quantity"], "id"),
            ("combos", Combo, lambda: Combo.objects.using("neon").all(), ["name", "price", "gst_percent", "image", "is_active", "created_at"], "id"),
            ("combo_items", ComboItem, lambda: ComboItem.objects.using("neon").all(), ["combo_id", "product_id", "quantity"], "id"),
            ("tables", Table, lambda: Table.objects.using("neon").all(), ["number", "floor", "capacity", "status"], "id"),
            ("table_sessions", TableSession, lambda: TableSession.objects.using("neon").all(), ["token_number", "table_id", "customer_name", "customer_phone", "guest_count", "is_active", "created_at", "closed_at"], "id"),
            ("orders", Order, lambda: Order.objects.using("neon").all(), ["order_type", "session_id", "table_id", "staff_id", "status", "payment_status", "total_amount", "discount_amount", "customer_id", "customer_name", "customer_phone", "bill_number", "order_number", "created_at"], "id"),
            ("order_items", OrderItem, lambda: OrderItem.objects.using("neon").all(), ["id", "order_id", "product_id", "combo_id", "quantity", "base_price", "gst_percent", "gst_amount", "price_at_time"], "id"),
            ("order_item_addons", OrderItemAddon, lambda: OrderItemAddon.objects.using("neon").all(), ["id", "order_item_id", "addon_id", "price_at_time"], "id"),
            ("payments", Payment, lambda: Payment.objects.using("neon").all(), ["order_id", "method", "amount", "status", "reference_id", "paid_at"], "id"),
            ("staff_session_logs", StaffSessionLog, lambda: StaffSessionLog.objects.using("neon").all(), ["user_id", "source", "login_at", "logout_at"], "id"),
            ("staff_report_access", StaffReportAccess, lambda: StaffReportAccess.objects.using("neon").all(), ["staff_user_id", "allowed_reports", "updated_at"], "id"),
        ]

        for name, model_class, queryset_factory, fields, pk_field in jobs:
            try:
                refreshed[name] = _mirror_queryset(
                    model_class,
                    queryset_factory(),
                    fields=fields,
                    pk_field=pk_field,
                )
            except (OperationalError, DatabaseError, InterfaceError) as exc:
                _reset_neon_connection()
                failed[name] = str(exc)[:250]
                logger.warning("SQLite mirror refresh failed for %s: %s", name, exc)
                if not is_neon_reachable(force=True):
                    return {"status": "offline", "refreshed": refreshed, "failed": failed}
            except Exception as exc:
                failed[name] = str(exc)[:250]
                logger.warning("Unexpected SQLite mirror refresh failure for %s: %s", name, exc)

        status = "ok" if not failed else "partial"
        return {"status": status, "refreshed": refreshed, "failed": failed}
    finally:
        _mirror_refresh_lock.release()


def trigger_background_sync(batch_size=BATCH_SIZE, max_batches=5):
    """
    Fire-and-forget sync trigger used by middleware.
    Avoids duplicate concurrent workers and throttles trigger frequency.
    """
    global _sync_in_progress, _last_triggered_at

    now = time.time()
    with _sync_trigger_lock:
        if _sync_in_progress:
            return {"status": "skipped", "reason": "already_running"}
        if (now - _last_triggered_at) < TRIGGER_COOLDOWN_SECONDS:
            return {"status": "skipped", "reason": "cooldown"}
        _sync_in_progress = True
        _last_triggered_at = now

    def _worker():
        global _sync_in_progress
        try:
            for _ in range(max_batches):
                result = sync_pending_records(batch_size=batch_size)
                if result.get("status") in {"offline", "idle"}:
                    break
                if result.get("synced", 0) == 0 and result.get("remaining", 0) == 0:
                    break
            refresh_sqlite_from_neon()
        except Exception:
            logger.exception("Background sync worker failed")
        finally:
            with _sync_trigger_lock:
                _sync_in_progress = False

    thread = threading.Thread(target=_worker, name="offline-sync-worker", daemon=True)
    thread.start()
    return {"status": "started"}


# ── internal dispatch ────────────────────────────────────────────────

def _process_item(item):
    """Route the queue item to the correct handler, writing to 'neon' DB."""
    from .models import SyncLog

    # Idempotency: skip if already synced to Neon
    if SyncLog.objects.using("neon").filter(client_id=item.client_id).exists():
        logger.info("Already synced client_id=%s — skipping.", item.client_id)
        return

    handler = _HANDLERS.get(f"{item.entity_type}/{item.action}")
    if handler is None:
        raise ValueError(f"No handler for {item.entity_type}/{item.action}")

    handler(item)


def _normalize_field_value(value):
    if hasattr(value, "name"):
        return value.name
    return value


def _reset_neon_connection():
    close_old_connections()
    try:
        connections["neon"].close()
    except Exception:
        pass


def _fetch_neon_batch(queryset, pk_field, last_pk=None):
    """
    Fetch one Neon batch without using queryset.iterator() server-side cursors.
    This is safer with pooled PostgreSQL connections.
    """
    attempts = 0
    while True:
        try:
            chunk = queryset
            if last_pk is not None:
                chunk = chunk.filter(**{f"{pk_field}__gt": last_pk})
            return list(chunk.order_by(pk_field)[:MIRROR_BATCH_SIZE])
        except (OperationalError, DatabaseError, InterfaceError):
            attempts += 1
            _reset_neon_connection()
            if attempts >= MIRROR_RETRY_ATTEMPTS:
                raise
            time.sleep(0.25 * attempts)


def _build_legacy_username(username, user_id):
    suffix = f"__legacy__{str(user_id).replace('-', '')[:8]}"
    base = (username or "legacy").strip()
    max_base_len = max(1, 150 - len(suffix))
    return f"{base[:max_base_len]}{suffix}"


def _build_legacy_ingredient_name(name, ingredient_id):
    suffix = f"__LEGACY__{str(ingredient_id).replace('-', '')[:8]}"
    base = (name or "INGREDIENT").strip().upper()
    max_base_len = max(1, 150 - len(suffix))
    return f"{base[:max_base_len]}{suffix}"


def _repoint_sqlite_user_relations(old_user_id, new_user_id):
    from accounts.models import User

    for rel in User._meta.related_objects:
        if getattr(rel, "many_to_many", False):
            continue
        attname = rel.field.attname
        related_model = rel.related_model
        try:
            related_model.objects.using("sqlite").filter(**{attname: old_user_id}).update(
                **{attname: new_user_id}
            )
        except Exception as exc:
            logger.warning(
                "Could not repoint sqlite relation %s.%s from user %s to %s: %s",
                related_model._meta.label_lower,
                attname,
                old_user_id,
                new_user_id,
                exc,
            )


def _repoint_sqlite_ingredient_relations(old_ingredient_id, new_ingredient_id):
    from inventory.models import Ingredient

    for rel in Ingredient._meta.related_objects:
        if getattr(rel, "many_to_many", False):
            continue
        attname = rel.field.attname
        related_model = rel.related_model
        try:
            related_model.objects.using("sqlite").filter(**{attname: old_ingredient_id}).update(
                **{attname: new_ingredient_id}
            )
        except Exception as exc:
            logger.warning(
                "Could not repoint sqlite relation %s.%s from ingredient %s to %s: %s",
                related_model._meta.label_lower,
                attname,
                old_ingredient_id,
                new_ingredient_id,
                exc,
            )


def _reconcile_user_identity_in_sqlite(sqlite_manager, server_user_id, defaults):
    """
    If local sqlite has the same username under a different UUID, migrate that row
    to the server UUID so child rows (staff_report_access, session logs, etc.)
    can keep strict FK integrity.
    """
    username = (defaults.get("username") or "").strip()
    if not username:
        return

    if sqlite_manager.filter(id=server_user_id).exists():
        return

    duplicate = sqlite_manager.filter(username=username).exclude(id=server_user_id).first()
    if not duplicate:
        return

    legacy_username = _build_legacy_username(duplicate.username, duplicate.id)

    with transaction.atomic(using="sqlite"):
        sqlite_manager.filter(id=duplicate.id).update(username=legacy_username)
        sqlite_manager.create(id=server_user_id, **defaults)
        _repoint_sqlite_user_relations(duplicate.id, server_user_id)
        sqlite_manager.filter(id=duplicate.id).delete()


def _reconcile_ingredient_identity_in_sqlite(sqlite_manager, server_ingredient_id, defaults):
    """
    If local sqlite has the same ingredient name under a different UUID, migrate that
    local row to the server UUID so downstream recipe rows keep FK integrity.
    """
    name = (defaults.get("name") or "").strip().upper()
    if not name:
        return

    if sqlite_manager.filter(id=server_ingredient_id).exists():
        return

    duplicate = (
        sqlite_manager
        .filter(name=name)
        .exclude(id=server_ingredient_id)
        .first()
    )
    if not duplicate:
        return

    legacy_name = _build_legacy_ingredient_name(duplicate.name, duplicate.id)

    with transaction.atomic(using="sqlite"):
        sqlite_manager.filter(id=duplicate.id).update(name=legacy_name)
        sqlite_manager.create(id=server_ingredient_id, **defaults)
        _repoint_sqlite_ingredient_relations(duplicate.id, server_ingredient_id)
        sqlite_manager.filter(id=duplicate.id).delete()


def _mirror_queryset(model_class, queryset, fields, pk_field="id"):
    """
    Upsert all rows from Neon into SQLite for a specific model.
    Returns the number of mirrored records.
    """
    count = 0
    skipped = 0
    sqlite_manager = model_class.objects.using("sqlite")

    last_pk = None
    while True:
        rows = _fetch_neon_batch(queryset, pk_field=pk_field, last_pk=last_pk)
        if not rows:
            break

        for row in rows:
            defaults = {
                key: _normalize_field_value(getattr(row, key))
                for key in fields
            }
            lookup = {pk_field: getattr(row, pk_field)}
            try:
                if model_class._meta.label_lower == "accounts.user":
                    _reconcile_user_identity_in_sqlite(
                        sqlite_manager,
                        server_user_id=lookup.get(pk_field),
                        defaults=defaults,
                    )
                elif model_class._meta.label_lower == "inventory.ingredient":
                    _reconcile_ingredient_identity_in_sqlite(
                        sqlite_manager,
                        server_ingredient_id=lookup.get(pk_field),
                        defaults=defaults,
                    )
                sqlite_manager.update_or_create(**lookup, defaults=defaults)
                count += 1
            except IntegrityError as exc:
                skipped += 1
                logger.warning(
                    "SQLite mirror integrity skip for %s (pk=%s): %s",
                    model_class._meta.label_lower,
                    lookup.get(pk_field),
                    exc,
                )
            except (OperationalError, DatabaseError) as exc:
                skipped += 1
                logger.warning(
                    "SQLite mirror row failed for %s (pk=%s): %s",
                    model_class._meta.label_lower,
                    lookup.get(pk_field),
                    exc,
                )
            except Exception as exc:
                skipped += 1
                logger.warning(
                    "SQLite mirror row skipped for %s (pk=%s): %s",
                    model_class._meta.label_lower,
                    lookup.get(pk_field),
                    exc,
                )

        last_pk = getattr(rows[-1], pk_field)

    if skipped:
        logger.info(
            "SQLite mirror completed with skipped rows for %s: mirrored=%s skipped=%s",
            model_class._meta.label_lower,
            count,
            skipped,
        )

    return count


# ── order/create ─────────────────────────────────────────────────────

def _sync_order_create(item):
    from accounts.models import Customer
    from orders.models import Order, OrderItem, OrderItemAddon
    from payments.models import Payment
    from products.models import Addon, Combo, Product
    from .models import SyncLog

    data = item.payload

    order_type = (data.get("order_type") or "TAKEAWAY").strip().upper()
    if order_type == "TAKE_AWAY":
        order_type = "TAKEAWAY"

    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = normalize_phone(data.get("customer_phone"))
    items_data = data.get("items", [])
    payment_data = data.get("payment")
    discount_amount = quantize_money(data.get("discount_amount") or "0")
    staff_id = data.get("staff_id")
    server_order_id = data.get("server_order_id")
    order_id = data.get("order_id")
    if not server_order_id and order_id:
        server_order_id = order_id
    requested_status = (data.get("status") or "COMPLETED").strip().upper()
    if requested_status not in {"NEW", "IN_PROGRESS", "READY", "SERVED", "COMPLETED", "CANCELLED"}:
        requested_status = "COMPLETED"
    requested_payment_status = (
        data.get("payment_status") or ("PAID" if payment_data else "UNPAID")
    ).strip().upper()
    if requested_payment_status not in {"UNPAID", "PAID", "REFUNDED"}:
        requested_payment_status = "UNPAID"

    with transaction.atomic(using="neon"):
        # Resolve / create customer
        customer = None
        if customer_phone:
            customer, _ = Customer.objects.using("neon").get_or_create(
                phone=customer_phone,
                defaults={"name": customer_name or "Customer"},
            )

        if server_order_id:
            order = Order.objects.using("neon").filter(id=server_order_id).first()
            if order is None:
                raise ValueError(f"Server order not found: {server_order_id}")
            was_paid_before = order.payment_status == "PAID"
            if order.payment_status == "PAID" or order.status in {"COMPLETED", "CANCELLED"}:
                raise ValueError("Cannot update paid, completed, or cancelled order")
            order.order_type = order_type
            order.customer = customer
            order.customer_name = customer_name
            order.customer_phone = customer_phone
            if staff_id:
                order.staff_id = staff_id
            order.discount_amount = discount_amount
            order.status = requested_status
            order.payment_status = requested_payment_status
            order.save(using="neon")
            OrderItemAddon.objects.using("neon").filter(order_item__order=order).delete()
            OrderItem.objects.using("neon").filter(order=order).delete()
        else:
            was_paid_before = False
            order = Order(
                id=order_id or None,
                order_type=order_type,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                staff_id=staff_id,
                discount_amount=discount_amount,
                status=requested_status,
                payment_status=requested_payment_status,
            )
            order.save(using="neon")

        subtotal = Decimal("0.00")

        for idata in items_data:
            product_id = idata.get("product_id") or idata.get("product")
            combo_id = idata.get("combo_id") or idata.get("combo")
            try:
                qty = parse_positive_quantity(idata.get("quantity", 1))
            except ValueError:
                qty = 1
            addons_list = idata.get("addons", []) or []

            product = combo = None
            menu_price = _safe_decimal(idata.get("base_price"))
            gst_percent = _safe_decimal(idata.get("gst_percent"))

            if product_id:
                try:
                    product = Product.objects.using("neon").get(id=product_id)
                    menu_price = product.price
                    gst_percent = Decimal(str(product.gst_percent or 0))
                except Product.DoesNotExist:
                    raise ValueError(f"Invalid product id: {product_id}")
            elif combo_id:
                try:
                    combo = Combo.objects.using("neon").get(id=combo_id)
                    menu_price = combo.price
                    gst_percent = Decimal(str(combo.gst_percent or 0))
                except Combo.DoesNotExist:
                    raise ValueError(f"Invalid combo id: {combo_id}")

            addon_total_per_unit = Decimal("0.00")
            addon_rows = []
            for addon_data in addons_list:
                addon_id = addon_data.get("addon") or addon_data.get("id")
                addon_qty = max(1, int(addon_data.get("quantity", 1)))
                if not addon_id:
                    continue
                addon_obj = Addon.objects.using("neon").filter(id=addon_id).first()
                if not addon_obj:
                    raise ValueError(f"Invalid addon id: {addon_id}")
                addon_total_per_unit += addon_obj.price * addon_qty
                addon_rows.append((addon_obj, addon_qty))

            amounts = calculate_line_amounts(
                menu_price=menu_price,
                gst_percent=gst_percent,
                addon_total=addon_total_per_unit,
            )
            base_price = quantize_money(idata.get("base_price") or amounts["base_price"])
            gst_percent = _safe_decimal(idata.get("gst_percent"), default=str(amounts["gst_percent"]))
            gst_amount = quantize_money(idata.get("gst_amount") or amounts["gst_amount"])
            unit_total = quantize_money(idata.get("price_at_time") or amounts["unit_total"])
            line_total = unit_total * qty

            oi = OrderItem(
                order=order,
                product=product,
                combo=combo,
                quantity=qty,
                base_price=base_price,
                gst_percent=gst_percent,
                gst_amount=gst_amount,
                price_at_time=unit_total,
            )
            oi.save(using="neon")
            subtotal += line_total

            for addon_obj, addon_qty in addon_rows:
                for _ in range(addon_qty):
                    OrderItemAddon(
                        order_item=oi,
                        addon=addon_obj,
                        price_at_time=addon_obj.price,
                    ).save(using="neon")

        final_total = calculate_payable_amount(subtotal, discount_amount)
        order.total_amount = final_total
        order.status = requested_status
        order.save(using="neon")

        if payment_data:
            method = (payment_data.get("method") or "CASH").upper()
            if method not in ("CASH", "CARD", "UPI"):
                method = "CASH"
            existing_payment = (
                Payment.objects.using("neon")
                .filter(order=order, status="SUCCESS")
                .order_by("-paid_at")
                .first()
            )
            if existing_payment:
                existing_payment.method = method
                existing_payment.amount = final_total
                existing_payment.reference_id = payment_data.get("reference", "") or ""
                existing_payment.save(using="neon", update_fields=["method", "amount", "reference_id"])
            else:
                Payment(
                    order=order,
                    method=method,
                    amount=final_total,
                    status="SUCCESS",
                    reference_id=payment_data.get("reference", ""),
                ).save(using="neon")
            order.payment_status = "PAID"
            order.save(using="neon", update_fields=["payment_status"])
            if not was_paid_before:
                _deduct_ingredients_for_paid_order(order, items_data=items_data)

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="order",
            action="update" if server_order_id else "create",
            server_id=order.id,
            response_data={
                "id": str(order.id),
                "total_amount": str(order.total_amount),
                "status": order.status,
                "payment_status": order.payment_status,
            },
        )

    logger.info("Synced order client_id=%s → server_id=%s", item.client_id, order.id)


# ── customer/create ──────────────────────────────────────────────────

def _sync_customer_create(item):
    from accounts.models import Customer
    from .models import SyncLog

    data = item.payload
    phone = normalize_phone(data.get("phone"))
    name = (data.get("name") or "Customer").strip()

    if not phone:
        raise ValueError("Phone is required for customer sync")

    with transaction.atomic(using="neon"):
        customer, created = Customer.objects.using("neon").get_or_create(
            phone=phone,
            defaults={"name": name},
        )

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="customer",
            action="create",
            server_id=customer.id,
            response_data={
                "id": str(customer.id),
                "name": customer.name,
                "phone": customer.phone,
                "created": created,
            },
        )

    logger.info("Synced customer client_id=%s → server_id=%s", item.client_id, customer.id)


# ── handler registry ─────────────────────────────────────────────────

def _sync_category_create(item):
    from products.models import Category
    from .models import SyncLog

    data = item.payload or {}
    category_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not category_id or not name:
        raise ValueError("Category id and name are required")

    with transaction.atomic(using="neon"):
        category, _ = Category.objects.using("neon").update_or_create(
            id=category_id,
            defaults={"name": name},
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="category",
            action="create",
            server_id=category.id,
            response_data={"id": str(category.id), "name": category.name},
        )


def _sync_product_create(item):
    from products.models import Category, Product
    from .models import SyncLog

    data = item.payload or {}
    product_id = data.get("id")
    name = (data.get("name") or "").strip()
    category_id = data.get("category_id")
    if not product_id or not name or not category_id:
        raise ValueError("Product id, name and category_id are required")

    category = Category.objects.using("neon").filter(id=category_id).first()
    if not category:
        raise ValueError(f"Category not found in neon: {category_id}")

    with transaction.atomic(using="neon"):
        product, _ = Product.objects.using("neon").update_or_create(
            id=product_id,
            defaults={
                "name": name,
                "category": category,
                "price": _safe_decimal(data.get("price")),
                "gst_percent": _safe_decimal(data.get("gst_percent")),
                "is_active": bool(data.get("is_active", True)),
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="product",
            action="create",
            server_id=product.id,
            response_data={"id": str(product.id), "name": product.name},
        )


def _sync_addon_create(item):
    from products.models import Addon
    from inventory.models import Ingredient
    from .models import SyncLog

    data = item.payload or {}
    addon_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not addon_id or not name:
        raise ValueError("Addon id and name are required")

    ingredient = None
    ingredient_id = data.get("ingredient_id")
    if ingredient_id:
        ingredient = Ingredient.objects.using("neon").filter(id=ingredient_id).first()

    with transaction.atomic(using="neon"):
        addon, _ = Addon.objects.using("neon").update_or_create(
            id=addon_id,
            defaults={
                "name": name,
                "price": _safe_decimal(data.get("price")),
                "ingredient": ingredient,
                "ingredient_quantity": _safe_decimal(
                    data.get("ingredient_quantity"),
                    default="0.000",
                ) if ingredient else Decimal("0.000"),
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="addon",
            action="create",
            server_id=addon.id,
            response_data={"id": str(addon.id), "name": addon.name},
        )


def _sync_combo_create(item):
    from products.models import Combo, ComboItem, Product
    from .models import SyncLog

    data = item.payload or {}
    combo_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not combo_id or not name:
        raise ValueError("Combo id and name are required")

    with transaction.atomic(using="neon"):
        combo, _ = Combo.objects.using("neon").update_or_create(
            id=combo_id,
            defaults={
                "name": name,
                "price": _safe_decimal(data.get("price")),
                "gst_percent": _safe_decimal(data.get("gst_percent")),
                "is_active": bool(data.get("is_active", True)),
            },
        )

        ComboItem.objects.using("neon").filter(combo_id=combo.id).delete()
        for row in (data.get("items") or []):
            product_id = row.get("product_id")
            if not product_id:
                continue
            product = Product.objects.using("neon").filter(id=product_id).first()
            if not product:
                continue
            ComboItem.objects.using("neon").create(
                combo=combo,
                product=product,
                quantity=max(1, _safe_int(row.get("quantity", 1), default=1)),
            )

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="combo",
            action="create",
            server_id=combo.id,
            response_data={"id": str(combo.id), "name": combo.name},
        )


def _sync_recipe_upsert(item):
    from inventory.models import Ingredient
    from products.models import Product, Recipe
    from .models import SyncLog

    data = item.payload or {}
    recipe_id = data.get("id")
    product_id = data.get("product_id")
    ingredient_id = data.get("ingredient_id")
    quantity = _safe_decimal(data.get("quantity"), default="0.000")

    if not recipe_id or not product_id or not ingredient_id:
        raise ValueError("Recipe id, product_id and ingredient_id are required")
    if quantity <= 0:
        raise ValueError("Recipe quantity must be greater than zero")

    product = Product.objects.using("neon").filter(id=product_id).first()
    ingredient = Ingredient.objects.using("neon").filter(id=ingredient_id).first()
    if not product or not ingredient:
        raise ValueError("Invalid product or ingredient for recipe sync")

    with transaction.atomic(using="neon"):
        recipe, _ = Recipe.objects.using("neon").update_or_create(
            id=recipe_id,
            defaults={
                "product_id": product_id,
                "ingredient_id": ingredient_id,
                "quantity": quantity,
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="recipe",
            action="upsert",
            server_id=None,
            response_data={
                "id": int(recipe.id),
                "product_id": str(recipe.product_id),
                "ingredient_id": str(recipe.ingredient_id),
                "quantity": str(recipe.quantity),
            },
        )


def _sync_recipe_delete(item):
    from products.models import Recipe
    from .models import SyncLog

    data = item.payload or {}
    recipe_id = data.get("id")
    if not recipe_id:
        raise ValueError("Recipe id is required")

    with transaction.atomic(using="neon"):
        Recipe.objects.using("neon").filter(id=recipe_id).delete()
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="recipe",
            action="delete",
            server_id=None,
            response_data={"id": int(recipe_id), "deleted": True},
        )

def _sync_system_reset(item):
    from accounts.services import perform_system_reset
    from .models import SyncLog

    superuser_id = item.payload.get("superuser_id")
    if not superuser_id:
        raise ValueError("superuser_id is required for system reset sync")

    with transaction.atomic(using="neon"):
        perform_system_reset(superuser_id=superuser_id, using="neon")
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="system",
            action="reset",
            server_id=None,
            response_data={"reset": True},
        )

    logger.info("Synced system reset client_id=%s", item.client_id)


def _sync_staff_create(item):
    from accounts.models import User
    from .models import SyncLog

    data = item.payload or {}
    user_id = data.get("id")
    username = (data.get("username") or "").strip()
    role = (data.get("role") or "STAFF").strip().upper()

    if not user_id or not username:
        raise ValueError("Staff id and username are required")

    with transaction.atomic(using="neon"):
        user, _ = User.objects.using("neon").update_or_create(
            id=user_id,
            defaults={
                "username": username,
                "first_name": (data.get("first_name") or "").strip(),
                "last_name": (data.get("last_name") or "").strip(),
                "email": (data.get("email") or "").strip(),
                "phone": (data.get("phone") or "").strip() or None,
                "role": role,
                "is_active": bool(data.get("is_active", True)),
                "is_staff": bool(data.get("is_staff", False)),
                "is_superuser": bool(data.get("is_superuser", False)),
                "password": data.get("password_hash") or "",
            },
        )

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="staff",
            action="create",
            server_id=user.id,
            response_data={
                "id": str(user.id),
                "username": user.username,
                "role": user.role,
            },
        )

    logger.info("Synced staff client_id=%s -> server_id=%s", item.client_id, user.id)


def _sync_ingredient_create(item):
    from inventory.models import (
        DEFAULT_INGREDIENT_CATEGORY_UUID,
        Ingredient,
        IngredientCategory,
    )
    from .models import SyncLog

    data = item.payload or {}
    ingredient_id = data.get("id")
    name = (data.get("name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not ingredient_id or not name or not unit:
        raise ValueError("Ingredient id, name and unit are required")

    with transaction.atomic(using="neon"):
        category_id = data.get("category_id") or str(DEFAULT_INGREDIENT_CATEGORY_UUID)
        category_name = (data.get("category_name") or "OTHERS").strip().upper()
        category, _ = IngredientCategory.objects.using("neon").update_or_create(
            id=category_id,
            defaults={"name": category_name or "OTHERS", "is_active": True},
        )
        ingredient, _ = Ingredient.objects.using("neon").update_or_create(
            id=ingredient_id,
            defaults={
                "name": name,
                "category": category,
                "unit": unit,
                "unit_price": _safe_decimal(data.get("unit_price"), default="0.00"),
                "current_stock": _safe_decimal(data.get("current_stock"), default="0.000"),
                "min_stock": _safe_decimal(data.get("min_stock"), default="0.000"),
                "is_active": bool(data.get("is_active", True)),
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="ingredient",
            action="create",
            server_id=ingredient.id,
            response_data={"id": str(ingredient.id), "name": ingredient.name},
        )


def _sync_ingredient_category_create(item):
    from inventory.models import IngredientCategory
    from .models import SyncLog

    data = item.payload or {}
    category_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not category_id or not name:
        raise ValueError("Ingredient category id and name are required")

    with transaction.atomic(using="neon"):
        category, _ = IngredientCategory.objects.using("neon").update_or_create(
            id=category_id,
            defaults={
                "name": name,
                "is_active": bool(data.get("is_active", True)),
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="ingredient_category",
            action="create",
            server_id=category.id,
            response_data={"id": str(category.id), "name": category.name},
        )


def _sync_ingredient_category_update(item):
    from inventory.models import IngredientCategory
    from .models import SyncLog

    data = item.payload or {}
    category_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not category_id or not name:
        raise ValueError("Ingredient category id and name are required")

    with transaction.atomic(using="neon"):
        category = IngredientCategory.objects.using("neon").filter(id=category_id).first()
        if category is None:
            raise ValueError("Ingredient category not found")
        category.name = name
        category.is_active = bool(data.get("is_active", category.is_active))
        category.save(using="neon")
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="ingredient_category",
            action="update",
            server_id=category.id,
            response_data={"id": str(category.id), "name": category.name},
        )


def _sync_ingredient_category_delete(item):
    from inventory.models import (
        DEFAULT_INGREDIENT_CATEGORY_UUID,
        Ingredient,
        IngredientCategory,
    )
    from .models import SyncLog

    data = item.payload or {}
    category_id = data.get("id")
    if not category_id:
        raise ValueError("Ingredient category id is required")

    with transaction.atomic(using="neon"):
        default_category, _ = IngredientCategory.objects.using("neon").get_or_create(
            id=DEFAULT_INGREDIENT_CATEGORY_UUID,
            defaults={"name": "OTHERS", "is_active": True},
        )
        if str(default_category.id) == str(category_id):
            raise ValueError("Default category cannot be deleted")

        category = IngredientCategory.objects.using("neon").filter(id=category_id).first()
        if category is not None:
            Ingredient.objects.using("neon").filter(category=category).update(category=default_category)
            category.delete(using="neon")

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="ingredient_category",
            action="delete",
            server_id=None,
            response_data={"id": str(category_id)},
        )


def _sync_vendor_create(item):
    from inventory.models import Vendor
    from .models import SyncLog

    data = item.payload or {}
    vendor_id = data.get("id")
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not vendor_id or not name:
        raise ValueError("Vendor id and name are required")

    with transaction.atomic(using="neon"):
        vendor, _ = Vendor.objects.using("neon").update_or_create(
            id=vendor_id,
            defaults={
                "name": name,
                "category": (data.get("category") or "").strip() or None,
                "contact_person": (data.get("contact_person") or "").strip() or None,
                "phone": phone,
                "email": (data.get("email") or "").strip() or None,
                "city": (data.get("city") or "").strip() or None,
                "address": (data.get("address") or "").strip() or None,
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="vendor",
            action="create",
            server_id=vendor.id,
            response_data={"id": str(vendor.id), "name": vendor.name},
        )


def _sync_opening_stock_init(item):
    from inventory.stock_service import upsert_daily_assignment
    from accounts.models import User
    from .models import SyncLog

    data = item.payload or {}
    items = data.get("items") or []
    set_by_id = data.get("set_by_id")
    user = User.objects.using("neon").filter(id=set_by_id).first() if set_by_id else None
    target_date_raw = data.get("date")
    target_date = parse_date(target_date_raw) if target_date_raw else timezone.localdate()
    if not target_date:
        target_date = timezone.localdate()

    assignment_items = [
        {
            "ingredient": row.get("ingredient_id") or row.get("ingredient"),
            "quantity": row.get("quantity"),
        }
        for row in items
    ]
    assigned_rows = upsert_daily_assignment(
        items=assignment_items,
        target_date=target_date,
        user=user,
        db_alias="neon",
    )

    SyncLog.objects.using("neon").create(
        client_id=item.client_id,
        entity_type="opening_stock",
        action="init",
        server_id=None,
        response_data={"count": len(assigned_rows), "date": target_date.isoformat()},
    )


_HANDLERS = {
    "order/create": _sync_order_create,
    "customer/create": _sync_customer_create,
    "system/reset": _sync_system_reset,
    "staff/create": _sync_staff_create,
    "ingredient_category/create": _sync_ingredient_category_create,
    "ingredient_category/update": _sync_ingredient_category_update,
    "ingredient_category/delete": _sync_ingredient_category_delete,
    "ingredient/create": _sync_ingredient_create,
    "vendor/create": _sync_vendor_create,
    "opening_stock/init": _sync_opening_stock_init,
    "category/create": _sync_category_create,
    "product/create": _sync_product_create,
    "addon/create": _sync_addon_create,
    "combo/create": _sync_combo_create,
    "recipe/upsert": _sync_recipe_upsert,
    "recipe/delete": _sync_recipe_delete,
}

