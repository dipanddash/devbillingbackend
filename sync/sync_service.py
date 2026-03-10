"""
Background sync service.

Reads pending records from the local OfflineSyncQueue (SQLite) and
pushes them to Neon PostgreSQL in small batches with retry logic.
"""

import logging
import uuid as uuid_mod
from decimal import Decimal, InvalidOperation

from django.db import models, transaction
from django.utils import timezone

from cafe_billing_backend.connectivity import is_neon_reachable

logger = logging.getLogger(__name__)

BATCH_SIZE = 10


# ── helpers ──────────────────────────────────────────────────────────

def _safe_decimal(value, default="0.00"):
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else Decimal(default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


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

    with transaction.atomic(using="neon"):
        # Resolve / create customer
        customer = None
        if customer_phone:
            customer, _ = Customer.objects.using("neon").get_or_create(
                phone=customer_phone,
                defaults={"name": customer_name or "Customer"},
            )

        # Create order — let Neon generate its own order_number
        order = Order(
            order_type=order_type,
            customer=customer,
            customer_name=customer_name,
            customer_phone=customer_phone,
            staff_id=staff_id,
            discount_amount=discount_amount,
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
        order.status = "COMPLETED"
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
            action="create",
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

_HANDLERS = {
    "order/create": _sync_order_create,
    "customer/create": _sync_customer_create,
}
