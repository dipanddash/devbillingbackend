"""
Sync API endpoints for offline-first architecture.

- GET  /api/sync/snapshot/  - Returns all POS-cacheable data in one call.
- POST /api/sync/push/     - Accepts batch of offline operations with idempotency.
- GET  /api/sync/health/   - Lightweight connectivity check for the frontend.
- GET  /api/sync/status/   - Returns offline mode flag and queue depth.
- POST /api/sync/trigger/  - Trigger a background sync batch from the frontend.
- POST /api/sync/queue/    - Enqueue an offline operation (used when offline).
"""

import logging
import uuid as uuid_mod
from decimal import Decimal, InvalidOperation

from django.db import DatabaseError, OperationalError, transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Customer
from accounts.permissions import IsAdminOrStaff
from orders.models import Order, OrderItem, OrderItemAddon
from orders.utils import format_bill_number, format_order_id
from payments.models import Payment
from products.models import Addon, Category, Combo, Product
from products.serializers import (
    AddonSerializer,
    CategorySerializer,
    ComboSerializer,
    ProductSerializer,
)
from tables.models import Table

from cafe_billing_backend.connectivity import is_neon_reachable
from .models import OfflineSyncQueue, SyncLog

logger = logging.getLogger(__name__)


class SyncHealthView(APIView):
    """Connectivity check used by the frontend to decide online/offline mode."""

    permission_classes = []
    authentication_classes = []

    def get(self, request):
        neon_online = is_neon_reachable(force=True)
        payload = {
            "status": "online" if neon_online else "offline",
            "neon_reachable": neon_online,
            "timestamp": timezone.now().isoformat(),
        }
        return Response(payload, status=200)


class SyncSnapshotView(APIView):
    """
    Returns all data needed for offline POS operation in a single call.
    Called once on login and periodically when online to keep cache fresh.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        preferred_alias = "neon" if is_neon_reachable(force=True) else "sqlite"
        payload, source_db = self._build_snapshot(request, preferred_alias)
        return Response(
            {
                **payload,
                "source_db": source_db,
                "server_time": timezone.now().isoformat(),
            }
        )

    def _build_snapshot(self, request, db_alias):
        try:
            categories = CategorySerializer(
                Category.objects.using(db_alias).all(),
                many=True,
                context={"request": request},
            ).data

            products = ProductSerializer(
                Product.objects.using(db_alias)
                .filter(is_active=True)
                .select_related("category")
                .prefetch_related("recipes__ingredient"),
                many=True,
                context={"request": request},
            ).data

            addons = AddonSerializer(
                Addon.objects.using(db_alias).all(),
                many=True,
                context={"request": request},
            ).data

            combos = ComboSerializer(
                Combo.objects.using(db_alias)
                .filter(is_active=True)
                .prefetch_related("items__product"),
                many=True,
                context={"request": request},
            ).data

            customers = list(
                Customer.objects.using(db_alias).values("id", "name", "phone", "created_at")
            )
            for customer in customers:
                customer["id"] = str(customer["id"])
                if customer.get("created_at"):
                    customer["created_at"] = customer["created_at"].isoformat()

            tables = list(
                Table.objects.using(db_alias).values("id", "number", "floor", "capacity", "status")
            )
            for table in tables:
                table["id"] = str(table["id"])

            return {
                "categories": categories,
                "products": products,
                "addons": addons,
                "combos": combos,
                "customers": customers,
                "tables": tables,
            }, db_alias
        except (OperationalError, DatabaseError):
            if db_alias == "sqlite":
                raise
            return self._build_snapshot(request, "sqlite")


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
    """

    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def post(self, request):
        operations = request.data.get("operations", [])
        if not isinstance(operations, list):
            return Response({"error": "operations must be a list"}, status=400)

        if not is_neon_reachable(force=True):
            return Response(
                {
                    "status": "queued_offline",
                    "results": self._queue_operations_locally(operations),
                },
                status=202,
            )

        results = []
        for op in operations:
            client_id = _safe_uuid(op.get("client_id"))
            if not client_id:
                results.append(
                    {
                        "client_id": str(op.get("client_id", "")),
                        "status": "error",
                        "message": "Invalid client_id",
                    }
                )
                continue

            entity_type = (op.get("entity_type") or "").strip().lower()
            action = (op.get("action") or "").strip().lower()
            data = op.get("data", {})

            existing = SyncLog.objects.using("neon").filter(client_id=client_id).first()
            if existing:
                results.append(
                    {
                        "client_id": str(client_id),
                        "status": "already_synced",
                        "server_id": str(existing.server_id) if existing.server_id else None,
                        "response_data": existing.response_data,
                    }
                )
                continue

            try:
                if entity_type == "order" and action == "create":
                    result = self._sync_order_create(client_id, data, request)
                elif entity_type == "customer" and action == "create":
                    result = self._sync_customer_create(client_id, data)
                elif entity_type == "staff" and action == "create":
                    result = self._sync_staff_create(client_id, data)
                elif entity_type == "ingredient" and action == "create":
                    result = self._sync_ingredient_create(client_id, data)
                elif entity_type == "vendor" and action == "create":
                    result = self._sync_vendor_create(client_id, data)
                elif entity_type == "opening_stock" and action == "init":
                    result = self._sync_opening_stock_init(client_id, data)
                elif entity_type == "system" and action == "reset":
                    result = self._sync_system_reset(client_id, data, request)
                else:
                    result = {
                        "client_id": str(client_id),
                        "status": "error",
                        "message": f"Unknown operation: {entity_type}/{action}",
                    }
            except Exception as exc:
                logger.exception("Sync push error for client_id=%s", client_id)
                result = {"client_id": str(client_id), "status": "error", "message": str(exc)}

            results.append(result)

        return Response({"results": results})

    def _queue_operations_locally(self, operations):
        queued = []
        for op in operations:
            cid = _safe_uuid(op.get("client_id"))
            if not cid:
                queued.append(
                    {
                        "client_id": str(op.get("client_id", "")),
                        "status": "error",
                        "message": "Invalid client_id",
                    }
                )
                continue

            entity_type = (op.get("entity_type") or "").strip().lower()
            action = (op.get("action") or "").strip().lower()
            if not entity_type or not action:
                queued.append(
                    {
                        "client_id": str(cid),
                        "status": "error",
                        "message": "entity_type and action are required",
                    }
                )
                continue

            if OfflineSyncQueue.objects.using("sqlite").filter(client_id=cid).exists():
                queued.append({"client_id": str(cid), "status": "already_queued"})
                continue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=cid,
                entity_type=entity_type,
                action=action,
                payload=op.get("data", {}) or {},
            )
            queued.append({"client_id": str(cid), "status": "queued"})

        return queued

    @transaction.atomic(using="neon")
    def _sync_order_create(self, client_id, data, request):
        user = request.user if request.user.is_authenticated else None

        order_type = (data.get("order_type") or "TAKEAWAY").strip().upper()
        if order_type == "TAKE_AWAY":
            order_type = "TAKEAWAY"

        customer_name = data.get("customer_name", "")
        customer_phone = data.get("customer_phone", "")
        items_data = data.get("items", [])
        payment_data = data.get("payment")
        discount_amount = _safe_decimal(data.get("discount_amount"))
        server_order_id = data.get("server_order_id")

        if not items_data:
            return {"client_id": str(client_id), "status": "error", "message": "No items provided"}

        customer = None
        if customer_phone:
            customer, _ = Customer.objects.using("neon").get_or_create(
                phone=customer_phone,
                defaults={"name": customer_name or "Customer"},
            )

        if server_order_id:
            order = Order.objects.using("neon").filter(id=server_order_id).first()
            if order is None:
                return {"client_id": str(client_id), "status": "error", "message": "Server order not found"}
            if order.payment_status == "PAID" or order.status in {"COMPLETED", "CANCELLED"}:
                return {"client_id": str(client_id), "status": "error", "message": "Order can no longer be modified"}
            order.order_type = order_type
            order.customer = customer
            order.customer_name = customer_name
            order.customer_phone = customer_phone
            order.staff_id = getattr(user, "id", None)
            order.discount_amount = discount_amount
            order.save(using="neon")
            OrderItemAddon.objects.using("neon").filter(order_item__order=order).delete()
            OrderItem.objects.using("neon").filter(order=order).delete()
        else:
            order = Order.objects.using("neon").create(
                order_type=order_type,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                staff_id=getattr(user, "id", None),
                discount_amount=discount_amount,
            )

        total = Decimal("0.00")
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
                    product = Product.objects.using("neon").get(id=product_id)
                    base_price = product.price
                    gst_percent = Decimal(str(product.gst_percent or 0))
                except Product.DoesNotExist:
                    continue
            elif combo_id:
                try:
                    combo = Combo.objects.using("neon").get(id=combo_id)
                    base_price = combo.price
                    gst_percent = Decimal(str(combo.gst_percent or 0))
                except Combo.DoesNotExist:
                    continue

            gst_amount = (base_price * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            line_total = base_price * qty

            order_item = OrderItem.objects.using("neon").create(
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

            for addon_data in addons_data:
                addon_id = addon_data.get("addon") or addon_data.get("id")
                addon_qty = max(1, int(addon_data.get("quantity", 1)))
                if not addon_id:
                    continue
                try:
                    addon_obj = Addon.objects.using("neon").get(id=addon_id)
                    for _ in range(addon_qty):
                        OrderItemAddon.objects.using("neon").create(
                            order_item=order_item,
                            addon=addon_obj,
                            price_at_time=addon_obj.price,
                        )
                    total += addon_obj.price * addon_qty * qty
                except Addon.DoesNotExist:
                    continue

        final_total = max(total - discount_amount, Decimal("0.00"))
        requested_status = (data.get("status") or "COMPLETED").strip().upper()
        if requested_status not in {"NEW", "IN_PROGRESS", "READY", "SERVED", "COMPLETED", "CANCELLED"}:
            requested_status = "COMPLETED"
        requested_payment_status = (data.get("payment_status") or ("PAID" if payment_data else "UNPAID")).strip().upper()
        if requested_payment_status not in {"UNPAID", "PAID", "REFUNDED"}:
            requested_payment_status = "UNPAID"

        order.total_amount = final_total
        order.status = requested_status
        order.payment_status = requested_payment_status
        order.save(using="neon")

        if payment_data:
            method = (payment_data.get("method") or "CASH").upper()
            if method not in ("CASH", "CARD", "UPI"):
                method = "CASH"

            Payment.objects.using("neon").create(
                order=order,
                method=method,
                amount=final_total,
                status="SUCCESS",
                reference_id=payment_data.get("reference", "") or "",
            )
            order.payment_status = "PAID"
            order.save(using="neon", update_fields=["payment_status"])

        response_data = {
            "id": str(order.id),
            "order_id": format_order_id(order.order_number),
            "bill_number": format_bill_number(order.bill_number),
            "total_amount": str(order.total_amount),
        }

        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="order",
            action="update" if server_order_id else "create",
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

        customer, created = Customer.objects.using("neon").get_or_create(
            phone=phone,
            defaults={"name": name or "Customer"},
        )

        response_data = {
            "id": str(customer.id),
            "name": customer.name,
            "phone": customer.phone,
            "created": created,
        }

        SyncLog.objects.using("neon").create(
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

    def _sync_system_reset(self, client_id, data, request):
        from accounts.services import perform_system_reset

        if not request.user.is_superuser:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Only superusers can perform a system reset.",
            }

        superuser_id = data.get("superuser_id") or str(request.user.id)
        perform_system_reset(superuser_id=superuser_id, using="neon")

        response_data = {"reset": True}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="system",
            action="reset",
            server_id=None,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": None,
            "response_data": response_data,
        }

    def _sync_staff_create(self, client_id, data):
        from accounts.models import User

        user_id = data.get("id")
        username = (data.get("username") or "").strip()
        role = (data.get("role") or "STAFF").strip().upper()

        if not user_id or not username:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Staff id and username are required",
            }

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

        response_data = {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
        }

        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="staff",
            action="create",
            server_id=user.id,
            response_data=response_data,
        )

        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": str(user.id),
            "response_data": response_data,
        }

    def _sync_ingredient_create(self, client_id, data):
        from inventory.models import Ingredient

        ingredient_id = data.get("id")
        name = (data.get("name") or "").strip()
        unit = (data.get("unit") or "").strip()
        if not ingredient_id or not name or not unit:
            return {"client_id": str(client_id), "status": "error", "message": "Ingredient id, name and unit are required"}

        ingredient, _ = Ingredient.objects.using("neon").update_or_create(
            id=ingredient_id,
            defaults={
                "name": name,
                "unit": unit,
                "current_stock": _safe_decimal(data.get("current_stock"), default="0.000"),
                "min_stock": _safe_decimal(data.get("min_stock"), default="0.000"),
            },
        )
        response_data = {"id": str(ingredient.id), "name": ingredient.name}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="ingredient",
            action="create",
            server_id=ingredient.id,
            response_data=response_data,
        )
        return {"client_id": str(client_id), "status": "synced", "server_id": str(ingredient.id), "response_data": response_data}

    def _sync_vendor_create(self, client_id, data):
        from inventory.models import Vendor

        vendor_id = data.get("id")
        name = (data.get("name") or "").strip()
        if not vendor_id or not name:
            return {"client_id": str(client_id), "status": "error", "message": "Vendor id and name are required"}

        vendor, _ = Vendor.objects.using("neon").update_or_create(
            id=vendor_id,
            defaults={
                "name": name,
                "category": (data.get("category") or "").strip() or None,
                "contact_person": (data.get("contact_person") or "").strip() or None,
                "phone": (data.get("phone") or "").strip(),
                "email": (data.get("email") or "").strip() or None,
                "city": (data.get("city") or "").strip() or None,
                "address": (data.get("address") or "").strip() or None,
            },
        )
        response_data = {"id": str(vendor.id), "name": vendor.name}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="vendor",
            action="create",
            server_id=vendor.id,
            response_data=response_data,
        )
        return {"client_id": str(client_id), "status": "synced", "server_id": str(vendor.id), "response_data": response_data}

    def _sync_opening_stock_init(self, client_id, data):
        from accounts.models import User
        from inventory.models import Ingredient, OpeningStock, StockLog

        items = data.get("items") or []
        set_by_id = data.get("set_by_id")
        user = User.objects.using("neon").filter(id=set_by_id).first() if set_by_id else None

        if OpeningStock.objects.using("neon").exists():
            response_data = {"count": 0, "already_initialized": True}
        else:
            openings = []
            logs = []
            touched = []
            for row in items:
                ingredient = Ingredient.objects.using("neon").filter(id=row.get("ingredient_id")).first()
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
            response_data = {"count": len(openings)}

        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="opening_stock",
            action="init",
            server_id=None,
            response_data=response_data,
        )
        return {"client_id": str(client_id), "status": "synced", "server_id": None, "response_data": response_data}


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

        return Response(
            {
                "offline_mode": not is_neon_reachable(force=True),
                "pending_sync": pending,
                "failed_sync": failed,
                "timestamp": timezone.now().isoformat(),
            }
        )


class SyncTriggerView(APIView):
    """Trigger one batch of offline -> Neon sync from the frontend."""

    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def post(self, request):
        from .sync_service import refresh_sqlite_from_neon, sync_pending_records

        batch_size = min(int(request.data.get("batch_size", 10)), 50)
        max_batches = min(int(request.data.get("max_batches", 10)), 20)

        total_synced = 0
        total_failed = 0
        remaining = 0
        status = "idle"

        for _ in range(max_batches):
            result = sync_pending_records(batch_size=batch_size)
            status = result.get("status", "idle")
            total_synced += result.get("synced", 0)
            total_failed += result.get("failed", 0)
            remaining = result.get("remaining", 0)

            if status in {"offline", "idle"} or remaining == 0:
                break

        refresh_result = refresh_sqlite_from_neon() if status != "offline" else {"status": "offline"}
        result = {
            "status": status if total_synced or total_failed or remaining else refresh_result.get("status", status),
            "synced": total_synced,
            "failed": total_failed,
            "remaining": remaining,
            "refresh": refresh_result,
        }
        return Response(result)


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
