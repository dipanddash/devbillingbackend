"""
Sync API endpoints for offline-first architecture.

- GET  /api/sync/snapshot/  — Returns all POS-cacheable data in one call.
- POST /api/sync/push/     — Accepts batch of offline operations with idempotency.
- GET  /api/sync/health/    — Lightweight connectivity check for the frontend.
- GET  /api/sync/status/    — Returns offline mode flag and queue depth.
- POST /api/sync/trigger/   — Trigger a background sync batch from the frontend.
- POST /api/sync/queue/     — Enqueue an offline operation (used when offline).
"""

import logging
import uuid as uuid_mod
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Customer
from accounts.permissions import IsAdminOrStaff
from orders.models import Order, OrderItem, OrderItemAddon
from orders.utils import format_order_id, format_bill_number
from payments.models import Payment
from products.models import Product, Addon, Combo, ComboItem, Category
from products.serializers import (
    CategorySerializer,
    ProductSerializer,
    AddonSerializer,
    ComboSerializer,
)
from tables.models import Table

from .models import SyncLog, OfflineSyncQueue

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HEALTH CHECK  (lightweight connectivity test)
# ─────────────────────────────────────────────

class SyncHealthView(APIView):
    """Returns 200 with server timestamp. Used by the frontend to detect connectivity."""
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        return Response({"status": "ok", "timestamp": timezone.now().isoformat()})


# ─────────────────────────────────────────────
# SNAPSHOT  (bulk data download for local cache)
# ─────────────────────────────────────────────

class SyncSnapshotView(APIView):
    """
    Returns all data needed for offline POS operation in a single call.
    Called once on login and periodically when online to keep cache fresh.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        categories = CategorySerializer(
            Category.objects.all(),
            many=True,
            context={"request": request},
        ).data

        products = ProductSerializer(
            Product.objects.filter(is_active=True).select_related("category").prefetch_related("recipes__ingredient"),
            many=True,
            context={"request": request},
        ).data

        addons = AddonSerializer(
            Addon.objects.all(),
            many=True,
            context={"request": request},
        ).data

        combos = ComboSerializer(
            Combo.objects.filter(is_active=True).prefetch_related("items__product"),
            many=True,
            context={"request": request},
        ).data

        # Customers — only basic fields for search/autocomplete
        customers = list(
            Customer.objects.values("id", "name", "phone", "created_at")
        )
        for c in customers:
            c["id"] = str(c["id"])
            if c.get("created_at"):
                c["created_at"] = c["created_at"].isoformat()

        # Tables — for dine-in reference
        tables = list(
            Table.objects.values("id", "number", "floor", "capacity", "status")
        )
        for t in tables:
            t["id"] = str(t["id"])

        return Response({
            "categories": categories,
            "products": products,
            "addons": addons,
            "combos": combos,
            "customers": customers,
            "tables": tables,
            "server_time": timezone.now().isoformat(),
        })


# ─────────────────────────────────────────────
# PUSH  (sync offline operations to server)
# ─────────────────────────────────────────────

def _safe_decimal(value, default="0.00"):
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else Decimal(default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _safe_uuid(value):
    try:
        return uuid_mod.UUID(str(value))
    except (ValueError, AttributeError):
        return None


class SyncPushView(APIView):
    """
    Accepts a batch of offline operations and processes them idempotently.

    Request body:
    {
      "operations": [
        {
          "client_id": "uuid",
          "entity_type": "order",
          "action": "create",
          "data": { ... full order payload ... }
        },
        ...
      ]
    }

    Each operation is checked against SyncLog by client_id.
    If already processed, the cached result is returned (idempotent).
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def post(self, request):
        operations = request.data.get("operations", [])
        if not isinstance(operations, list):
            return Response({"error": "operations must be a list"}, status=400)

        results = []
        for op in operations:
            client_id = _safe_uuid(op.get("client_id"))
            if not client_id:
                results.append({"client_id": str(op.get("client_id", "")), "status": "error", "message": "Invalid client_id"})
                continue

            entity_type = (op.get("entity_type") or "").strip().lower()
            action = (op.get("action") or "").strip().lower()
            data = op.get("data", {})

            # Idempotency check
            existing = SyncLog.objects.filter(client_id=client_id).first()
            if existing:
                results.append({
                    "client_id": str(client_id),
                    "status": "already_synced",
                    "server_id": str(existing.server_id) if existing.server_id else None,
                    "response_data": existing.response_data,
                })
                continue

            try:
                if entity_type == "order" and action == "create":
                    result = self._sync_order_create(client_id, data, request)
                elif entity_type == "customer" and action == "create":
                    result = self._sync_customer_create(client_id, data)
                else:
                    result = {"client_id": str(client_id), "status": "error", "message": f"Unknown operation: {entity_type}/{action}"}
            except Exception as exc:
                logger.exception("Sync push error for client_id=%s", client_id)
                result = {"client_id": str(client_id), "status": "error", "message": str(exc)}

            results.append(result)

        return Response({"results": results})

    @transaction.atomic
    def _sync_order_create(self, client_id, data, request):
        """
        Creates a complete order (with items, addons, payment) atomically.
        This combines the multi-step POS flow into a single sync operation.
        """
        user = request.user if request.user.is_authenticated else None

        order_type = (data.get("order_type") or "TAKEAWAY").strip().upper()
        if order_type == "TAKE_AWAY":
            order_type = "TAKEAWAY"

        customer_name = data.get("customer_name", "")
        customer_phone = data.get("customer_phone", "")
        items_data = data.get("items", [])
        payment_data = data.get("payment")
        discount_amount = _safe_decimal(data.get("discount_amount"))

        if not items_data:
            return {"client_id": str(client_id), "status": "error", "message": "No items provided"}

        # Resolve or create customer
        customer = None
        if customer_phone:
            customer, _ = Customer.objects.get_or_create(
                phone=customer_phone,
                defaults={"name": customer_name or "Customer"},
            )

        # Create order
        order = Order.objects.create(
            order_type=order_type,
            customer=customer,
            customer_name=customer_name,
            customer_phone=customer_phone,
            staff=user,
            discount_amount=discount_amount,
        )

        total = Decimal("0.00")

        # Create items
        for item_data in items_data:
            product_id = item_data.get("product") or item_data.get("productId")
            combo_id = item_data.get("combo") or item_data.get("comboId")
            qty = max(1, int(item_data.get("quantity", 1)))
            addons_data = item_data.get("addons", []) or []

            product = None
            combo = None
            base_price = Decimal("0.00")
            gst_percent = Decimal("0.00")

            if product_id:
                try:
                    product = Product.objects.get(id=product_id)
                    base_price = product.price
                    gst_percent = Decimal(str(product.gst_percent or 0))
                except Product.DoesNotExist:
                    continue  # Skip invalid products
            elif combo_id:
                try:
                    combo = Combo.objects.get(id=combo_id)
                    base_price = combo.price
                    gst_percent = Decimal(str(combo.gst_percent or 0))
                except Combo.DoesNotExist:
                    continue

            gst_amount = (base_price * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            line_total = base_price * qty

            order_item = OrderItem.objects.create(
                order=order,
                product=product,
                combo=combo,
                quantity=qty,
                base_price=base_price,
                gst_percent=gst_percent,
                gst_amount=gst_amount,
                price_at_time=base_price,
            )

            total += line_total

            # Create addons
            for addon_data in addons_data:
                addon_id = addon_data.get("addon") or addon_data.get("id")
                addon_qty = max(1, int(addon_data.get("quantity", 1)))
                if not addon_id:
                    continue
                try:
                    addon_obj = Addon.objects.get(id=addon_id)
                    for _ in range(addon_qty):
                        OrderItemAddon.objects.create(
                            order_item=order_item,
                            addon=addon_obj,
                            price_at_time=addon_obj.price,
                        )
                    total += addon_obj.price * addon_qty * qty
                except Addon.DoesNotExist:
                    continue

        # Apply discount
        final_total = max(total - discount_amount, Decimal("0.00"))
        order.total_amount = final_total
        order.status = "COMPLETED"
        order.save()

        # Process payment if provided
        if payment_data:
            method = (payment_data.get("method") or "CASH").upper()
            if method not in ("CASH", "CARD", "UPI"):
                method = "CASH"

            reference = payment_data.get("reference", "")
            Payment.objects.create(
                order=order,
                method=method,
                amount=final_total,
                status="SUCCESS",
                reference_id=reference or "",
            )
            order.payment_status = "PAID"
            order.save(update_fields=["payment_status"])

        # Log for idempotency
        response_data = {
            "id": str(order.id),
            "order_id": format_order_id(order.order_number),
            "bill_number": format_bill_number(order.bill_number),
            "total_amount": str(order.total_amount),
        }

        SyncLog.objects.create(
            client_id=client_id,
            entity_type="order",
            action="create",
            server_id=order.id,
            response_data=response_data,
        )

        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": str(order.id),
            "response_data": response_data,
        }

    def _sync_customer_create(self, client_id, data):
        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()

        if not phone:
            return {"client_id": str(client_id), "status": "error", "message": "Phone is required"}

        customer, created = Customer.objects.get_or_create(
            phone=phone,
            defaults={"name": name or "Customer"},
        )

        response_data = {
            "id": str(customer.id),
            "name": customer.name,
            "phone": customer.phone,
            "created": created,
        }

        SyncLog.objects.create(
            client_id=client_id,
            entity_type="customer",
            action="create",
            server_id=customer.id,
            response_data=response_data,
        )

        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": str(customer.id),
            "response_data": response_data,
        }


# ─────────────────────────────────────────────
# STATUS  (offline mode + queue depth)
# ─────────────────────────────────────────────

class SyncStatusView(APIView):
    """Returns the current offline mode status and pending queue count."""
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        from django.db import models as m
        pending = OfflineSyncQueue.objects.using("sqlite").filter(
            status="PENDING",
            retry_count__lt=m.F("max_retries"),
        ).count()
        failed = OfflineSyncQueue.objects.using("sqlite").filter(status="FAILED").count()

        return Response({
            "offline_mode": getattr(settings, "OFFLINE_MODE", False),
            "pending_sync": pending,
            "failed_sync": failed,
            "timestamp": timezone.now().isoformat(),
        })


# ─────────────────────────────────────────────
# TRIGGER  (kick off a background sync batch)
# ─────────────────────────────────────────────

class SyncTriggerView(APIView):
    """Trigger one batch of offline → Neon sync from the frontend."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def post(self, request):
        from .sync_service import sync_pending_records
        batch_size = min(int(request.data.get("batch_size", 10)), 50)
        result = sync_pending_records(batch_size=batch_size)
        return Response(result)


# ─────────────────────────────────────────────
# QUEUE  (enqueue an offline operation)
# ─────────────────────────────────────────────

class SyncQueueView(APIView):
    """
    Enqueue one or more operations into the local offline queue.
    Used by the frontend when it knows the system is offline.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        operations = request.data.get("operations", [])
        if not isinstance(operations, list):
            return Response({"error": "operations must be a list"}, status=400)

        queued = []
        for op in operations:
            cid = op.get("client_id")
            try:
                cid_uuid = uuid_mod.UUID(str(cid))
            except (ValueError, AttributeError):
                queued.append({"client_id": str(cid), "status": "error", "message": "Invalid client_id"})
                continue

            if OfflineSyncQueue.objects.using("sqlite").filter(client_id=cid_uuid).exists():
                queued.append({"client_id": str(cid_uuid), "status": "already_queued"})
                continue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=cid_uuid,
                entity_type=(op.get("entity_type") or "").strip().lower(),
                action=(op.get("action") or "").strip().lower(),
                payload=op.get("data", {}),
            )
            queued.append({"client_id": str(cid_uuid), "status": "queued"})

        return Response({"results": queued})
