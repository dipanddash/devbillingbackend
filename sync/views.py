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
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import DatabaseError, OperationalError, transaction
from django.db.utils import InterfaceError
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Customer
from accounts.permissions import IsAdminOrStaff
from orders.models import Order, OrderItem, OrderItemAddon
from orders.billing import (
    calculate_line_amounts,
    calculate_payable_amount,
    normalize_phone,
    parse_positive_quantity,
    quantize_money,
)
from orders.utils import format_bill_number, format_order_id
from payments.models import Payment
from products.models import Addon, Category, Combo, Product, Recipe
from products.billing_catalog import build_billing_catalog_payload
from products.serializers import (
    AddonSerializer,
    CategorySerializer,
    ComboSerializer,
    ProductSerializer,
)
from tables.models import Table
from inventory.models import Ingredient
from inventory.stock_service import consume_ingredients_for_sale, upsert_daily_assignment

from cafe_billing_backend.connectivity import is_neon_reachable
from .models import OfflineSyncQueue, SyncLog

logger = logging.getLogger(__name__)


class SyncHealthView(APIView):
    """Connectivity check used by the frontend to decide online/offline mode."""

    permission_classes = []
    authentication_classes = []

    @staticmethod
    def _db_probe(alias):
        from django.db import connections

        try:
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return {"ok": True}
        except (OperationalError, InterfaceError, DatabaseError) as exc:
            return {"ok": False, "error": str(exc).splitlines()[0][:180]}

    def get(self, request):
        quick_mode = str(request.GET.get("quick", "")).strip().lower() in {"1", "true", "yes"}
        neon_online = is_neon_reachable(force=not quick_mode)
        sqlite_status = {"ok": True} if quick_mode else self._db_probe("sqlite")
        default_alias = "sqlite" if settings.OFFLINE_MODE else "neon"
        default_status = sqlite_status if default_alias == "sqlite" else {"ok": neon_online}
        mode = "online" if neon_online else "offline"

        payload = {
            "status": mode,
            "mode": mode.upper(),
            "offline_mode": not neon_online,
            "sync_available": neon_online,
            "neon_reachable": neon_online,
            "db": {
                "default_alias": default_alias,
                "default": default_status,
                "sqlite": sqlite_status,
                "neon": {"ok": neon_online},
            },
            "probe_mode": "cached" if quick_mode else "full",
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

            categories = CategorySerializer(
                Category.objects.using(db_alias).all(),
                many=True,
                context={"request": request},
            ).data

            products = ProductSerializer(
                products_qs,
                many=True,
                context={"request": request},
            ).data

            addons = AddonSerializer(
                Addon.objects.using(db_alias).all(),
                many=True,
                context={"request": request},
            ).data

            combos = ComboSerializer(
                combos_qs,
                many=True,
                context={"request": request},
            ).data

            catalog_payload = build_billing_catalog_payload(db_alias=db_alias)
            billing_catalog = catalog_payload.get("items", [])
            billing_catalog_meta = catalog_payload.get("meta", {})

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
                "billing_catalog": billing_catalog,
                "billing_catalog_meta": billing_catalog_meta,
                "billing_catalog_updated_at": billing_catalog_meta.get("generated_at") or timezone.now().isoformat(),
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

def _build_recipe_map(product_ids):
    recipes_by_product = defaultdict(list)
    if not product_ids:
        return recipes_by_product

    recipes = (
        Recipe.objects.using("neon")
        .filter(product_id__in=product_ids)
        .select_related("ingredient", "product")
    )
    for recipe in recipes:
        recipes_by_product[recipe.product_id].append(recipe)
    return recipes_by_product


def _accumulate_addon_ingredient_usage(order_item, ingredient_usage):
    addon_counts = defaultdict(int)
    addons_by_id = {}
    addon_rows = order_item.addons.using("neon").all()
    for addon_row in addon_rows:
        addon = addon_row.addon
        if not addon or not addon.ingredient_id:
            continue
        addon_key = str(addon.id)
        addon_counts[addon_key] += 1
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
        ingredient_usage[addon.ingredient_id] += per_unit_qty * _safe_decimal(qty_per_item, default="0") * item_qty


def _ingredient_usage_from_payload(items_data):
    usage = defaultdict(Decimal)
    for item_data in items_data or []:
        try:
            qty = Decimal(str(item_data.get("quantity", 1)))
        except Exception:  # noqa: BLE001
            qty = Decimal("1")
        if qty <= 0:
            qty = Decimal("1")

        for blueprint in item_data.get("ingredient_blueprint", []) or []:
            ingredient_id = _safe_uuid(blueprint.get("ingredient_id"))
            if ingredient_id is None:
                continue
            per_unit = _safe_decimal(blueprint.get("quantity"), default="0.000")
            if per_unit <= 0:
                continue
            usage[ingredient_id] += per_unit * qty
    return usage


def _deduct_ingredients_for_paid_order(order, user, items_data=None):
    """
    Applies stock deduction for paid orders synced from offline queue.
    """
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
                    ingredient_usage[recipe.ingredient_id] += recipe.quantity * item.quantity
                continue

            if not item.combo_id or not item.combo:
                continue

            for combo_product in item.combo.items.using("neon").all():
                combo_recipes = recipes_by_product.get(combo_product.product_id, [])
                if not combo_recipes:
                    raise ValueError(f"No recipe configured for combo product {combo_product.product_id}")
                combined_qty = combo_product.quantity * item.quantity
                for recipe in combo_recipes:
                    ingredient_usage[recipe.ingredient_id] += recipe.quantity * combined_qty

            _accumulate_addon_ingredient_usage(item, ingredient_usage)

    consume_ingredients_for_sale(
        ingredient_usage=ingredient_usage,
        db_alias="neon",
        user=user,
        operation_date=timezone.localdate(),
    )


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
                elif entity_type == "ingredient_category" and action == "create":
                    result = self._sync_ingredient_category_create(client_id, data)
                elif entity_type == "ingredient_category" and action == "update":
                    result = self._sync_ingredient_category_update(client_id, data)
                elif entity_type == "ingredient_category" and action == "delete":
                    result = self._sync_ingredient_category_delete(client_id, data)
                elif entity_type == "ingredient" and action == "create":
                    result = self._sync_ingredient_create(client_id, data)
                elif entity_type == "vendor" and action == "create":
                    result = self._sync_vendor_create(client_id, data)
                elif entity_type == "opening_stock" and action == "init":
                    result = self._sync_opening_stock_init(client_id, data)
                elif entity_type == "recipe" and action == "upsert":
                    result = self._sync_recipe_upsert(client_id, data)
                elif entity_type == "recipe" and action == "delete":
                    result = self._sync_recipe_delete(client_id, data)
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

        customer_name = (data.get("customer_name") or "").strip()
        customer_phone = normalize_phone(data.get("customer_phone"))
        items_data = data.get("items", [])
        payment_data = data.get("payment")
        discount_amount = quantize_money(data.get("discount_amount") or "0")
        server_order_id = data.get("server_order_id")
        order_id = data.get("order_id")
        if not server_order_id and order_id:
            server_order_id = order_id

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
            was_paid_before = order.payment_status == "PAID"
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
            was_paid_before = False
            order = Order.objects.using("neon").create(
                id=order_id or None,
                order_type=order_type,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                staff_id=getattr(user, "id", None),
                discount_amount=discount_amount,
            )

        subtotal = Decimal("0.00")
        for item_data in items_data:
            product_id = item_data.get("product") or item_data.get("productId")
            combo_id = item_data.get("combo") or item_data.get("comboId")
            try:
                qty = parse_positive_quantity(item_data.get("quantity", 1))
            except ValueError:
                qty = 1
            addons_data = item_data.get("addons", []) or []

            product = None
            combo = None
            menu_price = Decimal("0.00")
            gst_percent = Decimal("0.00")

            if product_id:
                try:
                    product = Product.objects.using("neon").get(id=product_id)
                    menu_price = product.price
                    gst_percent = Decimal(str(product.gst_percent or 0))
                except Product.DoesNotExist:
                    return {
                        "client_id": str(client_id),
                        "status": "error",
                        "message": f"Invalid product id: {product_id}",
                    }
            elif combo_id:
                try:
                    combo = Combo.objects.using("neon").get(id=combo_id)
                    menu_price = combo.price
                    gst_percent = Decimal(str(combo.gst_percent or 0))
                except Combo.DoesNotExist:
                    return {
                        "client_id": str(client_id),
                        "status": "error",
                        "message": f"Invalid combo id: {combo_id}",
                    }

            addon_total_per_unit = Decimal("0.00")
            addon_rows = []
            for addon_data in addons_data:
                addon_id = addon_data.get("addon") or addon_data.get("id")
                addon_qty = max(1, int(addon_data.get("quantity", 1)))
                if not addon_id:
                    continue
                try:
                    addon_obj = Addon.objects.using("neon").get(id=addon_id)
                except Addon.DoesNotExist:
                    return {
                        "client_id": str(client_id),
                        "status": "error",
                        "message": f"Invalid addon id: {addon_id}",
                    }
                addon_total_per_unit += addon_obj.price * addon_qty
                addon_rows.append((addon_obj, addon_qty))

            amounts = calculate_line_amounts(
                menu_price=menu_price,
                gst_percent=gst_percent,
                addon_total=addon_total_per_unit,
            )
            base_price = quantize_money(item_data.get("base_price") or amounts["base_price"])
            gst_percent = _safe_decimal(item_data.get("gst_percent"), default=str(amounts["gst_percent"]))
            gst_amount = quantize_money(item_data.get("gst_amount") or amounts["gst_amount"])
            unit_total = quantize_money(item_data.get("price_at_time") or amounts["unit_total"])
            line_total = unit_total * qty

            order_item = OrderItem.objects.using("neon").create(
                order=order,
                product=product,
                combo=combo,
                quantity=qty,
                base_price=base_price,
                gst_percent=gst_percent,
                gst_amount=gst_amount,
                price_at_time=unit_total,
            )

            for addon_obj, addon_qty in addon_rows:
                for _ in range(addon_qty):
                    OrderItemAddon.objects.using("neon").create(
                        order_item=order_item,
                        addon=addon_obj,
                        price_at_time=addon_obj.price,
                    )

            subtotal += line_total

        final_total = calculate_payable_amount(subtotal, discount_amount)
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
                Payment.objects.using("neon").create(
                    order=order,
                    method=method,
                    amount=final_total,
                    status="SUCCESS",
                    reference_id=payment_data.get("reference", "") or "",
                )
            order.payment_status = "PAID"
            order.save(using="neon", update_fields=["payment_status"])
            if not was_paid_before:
                _deduct_ingredients_for_paid_order(order, user, items_data=items_data)

        response_data = {
            "id": str(order.id),
            "order_id": format_order_id(order.order_number),
            "bill_number": format_bill_number(order.bill_number),
            "total_amount": str(order.total_amount),
            "status": order.status,
            "payment_status": order.payment_status,
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
        phone = normalize_phone(data.get("phone"))

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
        from inventory.models import (
            DEFAULT_INGREDIENT_CATEGORY_UUID,
            Ingredient,
            IngredientCategory,
        )

        ingredient_id = data.get("id")
        name = (data.get("name") or "").strip()
        unit = (data.get("unit") or "").strip()
        if not ingredient_id or not name or not unit:
            return {"client_id": str(client_id), "status": "error", "message": "Ingredient id, name and unit are required"}

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
        response_data = {"id": str(ingredient.id), "name": ingredient.name}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="ingredient",
            action="create",
            server_id=ingredient.id,
            response_data=response_data,
        )
        return {"client_id": str(client_id), "status": "synced", "server_id": str(ingredient.id), "response_data": response_data}

    def _sync_ingredient_category_create(self, client_id, data):
        from inventory.models import IngredientCategory

        category_id = data.get("id")
        name = (data.get("name") or "").strip()
        if not category_id or not name:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Ingredient category id and name are required",
            }

        category, _ = IngredientCategory.objects.using("neon").update_or_create(
            id=category_id,
            defaults={
                "name": name,
                "is_active": bool(data.get("is_active", True)),
            },
        )
        response_data = {"id": str(category.id), "name": category.name}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="ingredient_category",
            action="create",
            server_id=category.id,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": str(category.id),
            "response_data": response_data,
        }

    def _sync_ingredient_category_update(self, client_id, data):
        from inventory.models import IngredientCategory

        category_id = data.get("id")
        name = (data.get("name") or "").strip()
        if not category_id or not name:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Ingredient category id and name are required",
            }

        category = IngredientCategory.objects.using("neon").filter(id=category_id).first()
        if category is None:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Ingredient category not found",
            }

        category.name = name
        category.is_active = bool(data.get("is_active", category.is_active))
        category.save(using="neon")
        response_data = {"id": str(category.id), "name": category.name}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="ingredient_category",
            action="update",
            server_id=category.id,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": str(category.id),
            "response_data": response_data,
        }

    def _sync_ingredient_category_delete(self, client_id, data):
        from inventory.models import (
            DEFAULT_INGREDIENT_CATEGORY_UUID,
            Ingredient,
            IngredientCategory,
        )

        category_id = data.get("id")
        if not category_id:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Ingredient category id is required",
            }

        default_category, _ = IngredientCategory.objects.using("neon").get_or_create(
            id=DEFAULT_INGREDIENT_CATEGORY_UUID,
            defaults={"name": "OTHERS", "is_active": True},
        )
        if str(default_category.id) == str(category_id):
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Default category cannot be deleted",
            }

        category = IngredientCategory.objects.using("neon").filter(id=category_id).first()
        if category:
            Ingredient.objects.using("neon").filter(category=category).update(category=default_category)
            category.delete(using="neon")

        response_data = {"id": str(category_id)}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="ingredient_category",
            action="delete",
            server_id=None,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": None,
            "response_data": response_data,
        }

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

        items = data.get("items") or []
        set_by_id = data.get("set_by_id")
        user = User.objects.using("neon").filter(id=set_by_id).first() if set_by_id else None
        target_date_raw = data.get("date")
        target_date = parse_date(target_date_raw) if target_date_raw else timezone.localdate()
        if not target_date:
            target_date = timezone.localdate()

        assigned_rows = upsert_daily_assignment(
            items=[
                {
                    "ingredient": row.get("ingredient_id") or row.get("ingredient"),
                    "quantity": row.get("quantity"),
                }
                for row in items
            ],
            target_date=target_date,
            user=user,
            db_alias="neon",
        )
        response_data = {"count": len(assigned_rows), "date": target_date.isoformat()}

        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="opening_stock",
            action="init",
            server_id=None,
            response_data=response_data,
        )
        return {"client_id": str(client_id), "status": "synced", "server_id": None, "response_data": response_data}

    def _sync_recipe_upsert(self, client_id, data):
        recipe_id = data.get("id")
        product_id = data.get("product_id")
        ingredient_id = data.get("ingredient_id")
        quantity = _safe_decimal(data.get("quantity"), default="0.000")

        if not recipe_id or not product_id or not ingredient_id:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Recipe id, product_id and ingredient_id are required",
            }
        if quantity <= 0:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Recipe quantity must be greater than zero",
            }

        product = Product.objects.using("neon").filter(id=product_id).first()
        ingredient = Ingredient.objects.using("neon").filter(id=ingredient_id).first()
        if not product or not ingredient:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Invalid product or ingredient",
            }

        recipe, _ = Recipe.objects.using("neon").update_or_create(
            id=recipe_id,
            defaults={
                "product_id": product_id,
                "ingredient_id": ingredient_id,
                "quantity": quantity,
            },
        )
        response_data = {
            "id": int(recipe.id),
            "product_id": str(recipe.product_id),
            "ingredient_id": str(recipe.ingredient_id),
            "quantity": str(recipe.quantity),
        }
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="recipe",
            action="upsert",
            server_id=None,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": None,
            "response_data": response_data,
        }

    def _sync_recipe_delete(self, client_id, data):
        recipe_id = data.get("id")
        if not recipe_id:
            return {
                "client_id": str(client_id),
                "status": "error",
                "message": "Recipe id is required",
            }

        Recipe.objects.using("neon").filter(id=recipe_id).delete()
        response_data = {"id": int(recipe_id), "deleted": True}
        SyncLog.objects.using("neon").create(
            client_id=client_id,
            entity_type="recipe",
            action="delete",
            server_id=None,
            response_data=response_data,
        )
        return {
            "client_id": str(client_id),
            "status": "synced",
            "server_id": None,
            "response_data": response_data,
        }


class SyncStatusView(APIView):
    """Returns the current offline mode status and pending queue count."""

    permission_classes = []
    authentication_classes = []

    def get(self, request):
        from django.db import models as m

        in_progress = OfflineSyncQueue.objects.using("sqlite").filter(status="IN_PROGRESS").count()
        pending = OfflineSyncQueue.objects.using("sqlite").filter(
            status="PENDING",
            retry_count__lt=m.F("max_retries"),
        ).count()
        failed = OfflineSyncQueue.objects.using("sqlite").filter(status="FAILED").count()
        offline_mode = not is_neon_reachable(force=True)

        if offline_mode and (pending or in_progress or failed):
            state = "OFFLINE_PENDING_SYNC"
        elif offline_mode:
            state = "OFFLINE_SYNCED"
        elif in_progress:
            state = "SYNCING"
        elif failed:
            state = "SYNC_FAILED"
        elif pending:
            state = "ONLINE_PENDING_SYNC"
        else:
            state = "ONLINE_SYNCED"

        return Response(
            {
                "state": state,
                "offline_mode": offline_mode,
                "sync_available": not offline_mode,
                "in_progress_sync": in_progress,
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
