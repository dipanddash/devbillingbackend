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
from django.utils import timezone

from cafe_billing_backend.connectivity import is_neon_reachable

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
TRIGGER_COOLDOWN_SECONDS = 10

_sync_trigger_lock = threading.Lock()
_sync_in_progress = False
_last_triggered_at = 0.0


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
    if not is_neon_reachable(force=True):
        return {"status": "offline", "refreshed": {}}

    from accounts.models import Customer, StaffReportAccess, StaffSessionLog, User
    from orders.models import Order, OrderItem, OrderItemAddon
    from payments.models import Payment
    from products.models import Addon, Category, Combo, ComboItem, Product
    from tables.models import Table, TableSession

    refreshed = {}
    failed = {}

    # Pull parent tables first, then dependents.
    jobs = [
        ("users", User, User.objects.using("neon").all(), [
            "username", "password", "first_name", "last_name", "email",
            "is_staff", "is_active", "is_superuser", "last_login",
            "date_joined", "role", "phone", "day_locked_on",
        ], "id"),
        ("customers", Customer, Customer.objects.using("neon").all(), ["name", "phone", "created_at"], "id"),
        ("categories", Category, Category.objects.using("neon").all(), ["name", "image"], "id"),
        ("products", Product, Product.objects.using("neon").select_related("category"), ["name", "category_id", "price", "gst_percent", "image", "is_active"], "id"),
        ("addons", Addon, Addon.objects.using("neon").all(), ["name", "price", "image"], "id"),
        ("combos", Combo, Combo.objects.using("neon").all(), ["name", "price", "gst_percent", "image", "is_active", "created_at"], "id"),
        ("combo_items", ComboItem, ComboItem.objects.using("neon").all(), ["combo_id", "product_id", "quantity"], "id"),
        ("tables", Table, Table.objects.using("neon").all(), ["number", "floor", "capacity", "status"], "id"),
        ("table_sessions", TableSession, TableSession.objects.using("neon").all(), ["token_number", "table_id", "customer_name", "customer_phone", "guest_count", "is_active", "created_at", "closed_at"], "id"),
        ("orders", Order, Order.objects.using("neon").all(), ["order_type", "session_id", "table_id", "staff_id", "status", "payment_status", "total_amount", "discount_amount", "customer_id", "customer_name", "customer_phone", "bill_number", "order_number", "created_at"], "id"),
        ("order_items", OrderItem, OrderItem.objects.using("neon").all(), ["id", "order_id", "product_id", "combo_id", "quantity", "base_price", "gst_percent", "gst_amount", "price_at_time"], "id"),
        ("order_item_addons", OrderItemAddon, OrderItemAddon.objects.using("neon").all(), ["id", "order_item_id", "addon_id", "price_at_time"], "id"),
        ("payments", Payment, Payment.objects.using("neon").all(), ["order_id", "method", "amount", "status", "reference_id", "paid_at"], "id"),
        ("staff_session_logs", StaffSessionLog, StaffSessionLog.objects.using("neon").all(), ["user_id", "source", "login_at", "logout_at"], "id"),
        ("staff_report_access", StaffReportAccess, StaffReportAccess.objects.using("neon").all(), ["staff_user_id", "allowed_reports", "updated_at"], "id"),
    ]

    for name, model_class, queryset, fields, pk_field in jobs:
        try:
            refreshed[name] = _mirror_queryset(
                model_class,
                queryset,
                fields=fields,
                pk_field=pk_field,
            )
        except (OperationalError, DatabaseError) as exc:
            _reset_neon_connection()
            failed[name] = str(exc)[:250]
            logger.warning("SQLite mirror refresh failed for %s: %s", name, exc)
            if not is_neon_reachable(force=True):
                return {"status": "offline", "refreshed": refreshed, "failed": failed}
        except Exception as exc:
            failed[name] = str(exc)[:250]
            logger.exception("Unexpected SQLite mirror refresh failure for %s", name)

    status = "ok" if not failed else "partial"
    return {"status": status, "refreshed": refreshed, "failed": failed}


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


def _mirror_queryset(model_class, queryset, fields, pk_field="id"):
    """
    Upsert all rows from Neon into SQLite for a specific model.
    Returns the number of mirrored records.
    """
    count = 0
    sqlite_manager = model_class.objects.using("sqlite")
    _reset_neon_connection()

    for row in queryset.iterator():
        defaults = {
            key: _normalize_field_value(getattr(row, key))
            for key in fields
        }
        lookup = {pk_field: getattr(row, pk_field)}
        sqlite_manager.update_or_create(**lookup, defaults=defaults)
        count += 1

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

    customer_name = data.get("customer_name", "")
    customer_phone = data.get("customer_phone", "")
    items_data = data.get("items", [])
    payment_data = data.get("payment")
    discount_amount = _safe_decimal(data.get("discount_amount"))
    staff_id = data.get("staff_id")
    server_order_id = data.get("server_order_id")
    requested_status = (data.get("status") or "COMPLETED").strip().upper()
    if requested_status not in {"NEW", "IN_PROGRESS", "READY", "SERVED", "COMPLETED", "CANCELLED"}:
        requested_status = "COMPLETED"
    requested_payment_status = (data.get("payment_status") or "PAID").strip().upper()
    if requested_payment_status not in {"UNPAID", "PAID", "REFUNDED"}:
        requested_payment_status = "PAID"

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
            # Create order — let Neon generate its own order_number
            order = Order(
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

        total = Decimal("0.00")

        for idata in items_data:
            product_id = idata.get("product_id") or idata.get("product")
            combo_id = idata.get("combo_id") or idata.get("combo")
            qty = max(1, int(idata.get("quantity", 1)))
            addons_list = idata.get("addons", []) or []

            product = combo = None
            base_price = _safe_decimal(idata.get("base_price"))
            gst_percent = _safe_decimal(idata.get("gst_percent"))

            if product_id:
                try:
                    product = Product.objects.using("neon").get(id=product_id)
                    base_price = product.price
                    gst_percent = Decimal(str(product.gst_percent or 0))
                except Product.DoesNotExist:
                    pass
            elif combo_id:
                try:
                    combo = Combo.objects.using("neon").get(id=combo_id)
                    base_price = combo.price
                    gst_percent = Decimal(str(combo.gst_percent or 0))
                except Combo.DoesNotExist:
                    pass

            gst_amount = (base_price * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            line_total = base_price * qty

            oi = OrderItem(
                order=order,
                product=product,
                combo=combo,
                quantity=qty,
                base_price=base_price,
                gst_percent=gst_percent,
                gst_amount=gst_amount,
                price_at_time=base_price,
            )
            oi.save(using="neon")
            total += line_total

            for addon_data in addons_list:
                addon_id = addon_data.get("addon") or addon_data.get("id")
                addon_qty = max(1, int(addon_data.get("quantity", 1)))
                if not addon_id:
                    continue
                try:
                    addon_obj = Addon.objects.using("neon").get(id=addon_id)
                    for _ in range(addon_qty):
                        OrderItemAddon(
                            order_item=oi,
                            addon=addon_obj,
                            price_at_time=addon_obj.price,
                        ).save(using="neon")
                    total += addon_obj.price * addon_qty * qty
                except Addon.DoesNotExist:
                    continue

        final_total = max(total - discount_amount, Decimal("0.00"))
        order.total_amount = final_total
        order.status = requested_status
        order.save(using="neon")

        if payment_data:
            method = (payment_data.get("method") or "CASH").upper()
            if method not in ("CASH", "CARD", "UPI"):
                method = "CASH"
            Payment(
                order=order,
                method=method,
                amount=final_total,
                status="SUCCESS",
                reference_id=payment_data.get("reference", ""),
            ).save(using="neon")
            order.payment_status = "PAID"
            order.save(using="neon", update_fields=["payment_status"])

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="order",
            action="update" if server_order_id else "create",
            server_id=order.id,
            response_data={"id": str(order.id), "total_amount": str(order.total_amount)},
        )

    logger.info("Synced order client_id=%s → server_id=%s", item.client_id, order.id)


# ── customer/create ──────────────────────────────────────────────────

def _sync_customer_create(item):
    from accounts.models import Customer
    from .models import SyncLog

    data = item.payload
    phone = (data.get("phone") or "").strip()
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
    from .models import SyncLog

    data = item.payload or {}
    addon_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not addon_id or not name:
        raise ValueError("Addon id and name are required")

    with transaction.atomic(using="neon"):
        addon, _ = Addon.objects.using("neon").update_or_create(
            id=addon_id,
            defaults={
                "name": name,
                "price": _safe_decimal(data.get("price")),
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
    from inventory.models import Ingredient
    from .models import SyncLog

    data = item.payload or {}
    ingredient_id = data.get("id")
    name = (data.get("name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not ingredient_id or not name or not unit:
        raise ValueError("Ingredient id, name and unit are required")

    with transaction.atomic(using="neon"):
        ingredient, _ = Ingredient.objects.using("neon").update_or_create(
            id=ingredient_id,
            defaults={
                "name": name,
                "unit": unit,
                "current_stock": _safe_decimal(data.get("current_stock"), default="0.000"),
                "min_stock": _safe_decimal(data.get("min_stock"), default="0.000"),
            },
        )
        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="ingredient",
            action="create",
            server_id=ingredient.id,
            response_data={"id": str(ingredient.id), "name": ingredient.name},
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
    from inventory.models import Ingredient, OpeningStock, StockLog
    from accounts.models import User
    from .models import SyncLog

    data = item.payload or {}
    items = data.get("items") or []
    set_by_id = data.get("set_by_id")
    user = User.objects.using("neon").filter(id=set_by_id).first() if set_by_id else None

    if OpeningStock.objects.using("neon").exists():
        return

    openings = []
    logs = []
    touched = []
    for row in items:
        ingredient_id = row.get("ingredient_id")
        ingredient = Ingredient.objects.using("neon").filter(id=ingredient_id).first()
        if not ingredient:
            continue
        qty = _safe_decimal(row.get("quantity"), default="0.000")
        ingredient.current_stock = qty
        touched.append(ingredient)
        openings.append(OpeningStock(ingredient=ingredient, quantity=qty, set_by=user))
        logs.append(StockLog(ingredient=ingredient, change=qty, reason="OPENING", user=user))

    with transaction.atomic(using="neon"):
        if openings:
            OpeningStock.objects.using("neon").bulk_create(openings)
            Ingredient.objects.using("neon").bulk_update(touched, ["current_stock"])
            StockLog.objects.using("neon").bulk_create(logs)

        SyncLog.objects.using("neon").create(
            client_id=item.client_id,
            entity_type="opening_stock",
            action="init",
            server_id=None,
            response_data={"count": len(openings)},
        )


_HANDLERS = {
    "order/create": _sync_order_create,
    "customer/create": _sync_customer_create,
    "system/reset": _sync_system_reset,
    "staff/create": _sync_staff_create,
    "ingredient/create": _sync_ingredient_create,
    "vendor/create": _sync_vendor_create,
    "opening_stock/init": _sync_opening_stock_init,
    "category/create": _sync_category_create,
    "product/create": _sync_product_create,
    "addon/create": _sync_addon_create,
    "combo/create": _sync_combo_create,
}

