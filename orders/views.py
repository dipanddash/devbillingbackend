from decimal import Decimal
from datetime import datetime, timedelta
from collections import defaultdict
import json
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum, F, DecimalField, Prefetch, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import requests
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from accounts.models import Customer
from accounts.permissions import IsAdminOrStaff, IsAdminRole
from payments.models import Payment
from inventory.models import StockLog, Ingredient
from products.models import Product, Recipe, Combo, Addon
from tables.models import TableSession

from .models import Order, OrderItem, OrderItemAddon, Coupon, CouponUsage
from .serializers import (
    OrderSerializer,
    OrderStatusSerializer,
    KitchenOrderSerializer,
    OrderListSerializer,
    OrderDetailSerializer,
    CouponSerializer,
    CouponUsageSerializer,
)
from .utils import format_order_id, format_bill_number

logger = logging.getLogger(__name__)


def _parse_positive_quantity(value):
    if isinstance(value, bool):
        raise ValueError("Quantity must be a valid integer")

    if isinstance(value, int):
        qty = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("Quantity must be a valid integer")
        qty = int(raw)
    else:
        raise ValueError("Quantity must be a valid integer")

    if qty <= 0:
        raise ValueError("Quantity must be greater than 0")
    return qty


def _parse_non_negative_amount(value):
    if value in (None, ""):
        raise ValueError("Amount is required")
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Amount must be numeric") from exc
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return amount


def _normalize_phone_number(phone):
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) < 10:
        return None
    return digits[-10:]


def send_fast2sms_whatsapp_message(phone, variables_values, message_id=None):
    api_key = (getattr(settings, "FAST2SMS_API_KEY", "") or "").strip()
    template_id = (message_id or getattr(settings, "FAST2SMS_WHATSAPP_TEMPLATE_ID", "") or "").strip()
    mobile_no = _normalize_phone_number(phone)

    if not api_key or not template_id or not mobile_no:
        return {
            "sent": False,
            "reason": "missing_api_key_or_template_or_phone",
            "meta": {
                "has_api_key": bool(api_key),
                "has_template_id": bool(template_id),
                "mobile_no": mobile_no,
            },
        }

    url = "https://www.fast2sms.com/dev/whatsapp"
    payload = {
        "message_id": str(template_id),
        # Fast2SMS WhatsApp API expects "numbers" (not only "mobile_no")
        # in current versions of the endpoint.
        "numbers": mobile_no,
        "mobile_no": mobile_no,
        "variables_values": variables_values,
    }
    headers = {
        "authorization": api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=12)
        response_body = {}
        if response.content:
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"raw": response.text[:500]}

        provider_ok = response.ok and not (
            isinstance(response_body, dict) and response_body.get("return") is False
        )
        provider_message = None
        if isinstance(response_body, dict):
            provider_message = (
                response_body.get("message")
                or response_body.get("error")
                or response_body.get("msg")
            )
            # Some providers send list/dict under errors/details.
            if not provider_message and response_body.get("errors") is not None:
                provider_message = str(response_body.get("errors"))
            if not provider_message and response_body.get("details") is not None:
                provider_message = str(response_body.get("details"))
        return {
            "sent": provider_ok,
            "status_code": response.status_code,
            "reason": None if provider_ok else "provider_rejected",
            "provider_message": provider_message,
            "body": response_body,
        }
    except requests.RequestException as exc:
        logger.exception("Fast2SMS WhatsApp send failed: %s", exc)
        return {"sent": False, "reason": "request_exception", "detail": str(exc)}


def send_order_invoice_whatsapp(order, bill_number, final_amount, method):
    customer_name = order.customer_name or (order.customer.name if order.customer else "") or "Customer"
    customer_phone = order.customer_phone or (order.customer.phone if order.customer else None)
    date_str = timezone.localtime(order.created_at).strftime("%d-%m-%Y")
    variables_values = f"{customer_name}|{bill_number}|{final_amount}|{method}|{date_str}"

    result = send_fast2sms_whatsapp_message(
        phone=customer_phone,
        variables_values=variables_values,
    )
    if not result.get("sent"):
        logger.warning(
            "WhatsApp invoice send skipped/failed for order=%s bill=%s result=%s",
            order.id,
            bill_number,
            result,
        )
    return result


def apply_order_filters(request, queryset):
    """
    Supported query params:
    - filter=pending|cancelled|paid|finished
    - status=NEW,IN_PROGRESS,...
    - payment_status=UNPAID,PAID,REFUNDED
    """
    filter_key = (request.GET.get("filter") or "").strip().lower()
    status_param = (request.GET.get("status") or "").strip()
    payment_param = (request.GET.get("payment_status") or "").strip()

    if filter_key == "pending":
        queryset = queryset.exclude(status__in=["CANCELLED", "COMPLETED"]).filter(payment_status="UNPAID")
    elif filter_key == "cancelled":
        queryset = queryset.filter(status="CANCELLED")
    elif filter_key == "paid":
        queryset = queryset.filter(payment_status="PAID")
    elif filter_key == "finished":
        queryset = queryset.filter(status="COMPLETED")

    if status_param:
        statuses = [s.strip() for s in status_param.split(",") if s.strip()]
        if statuses:
            queryset = queryset.filter(status__in=statuses)

    if payment_param:
        payments = [p.strip() for p in payment_param.split(",") if p.strip()]
        if payments:
            queryset = queryset.filter(payment_status__in=payments)

    return queryset


def error_response(message, status_code, extra=None):
    payload = {"error": message, "detail": message}
    if extra:
        payload.update(extra)
    return Response(payload, status=status_code)


def _compute_coupon_discount(coupon, order_amount):
    if order_amount < (coupon.min_order_amount or Decimal("0")):
        return Decimal("0.00")

    if coupon.discount_type == "FREE_ITEM":
        return Decimal("0.00")
    if coupon.discount_type == "PERCENT":
        discount = (order_amount * Decimal(str(coupon.value))) / Decimal("100")
    else:
        discount = Decimal(str(coupon.value))

    max_discount = coupon.max_discount_amount
    if max_discount is not None and Decimal(str(max_discount)) > 0:
        discount = min(discount, Decimal(str(max_discount)))

    if discount < 0:
        discount = Decimal("0.00")
    return min(discount, order_amount)


class CouponListCreateView(generics.ListCreateAPIView):
    queryset = Coupon.objects.all().annotate(used=Count("usage_records")).order_by("-created_at")
    serializer_class = CouponSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class CouponRetrieveUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Coupon.objects.all()
    serializer_class = CouponSerializer

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]


class CouponUsageListView(generics.ListAPIView):
    serializer_class = CouponUsageSerializer
    permission_classes = [IsAdminRole]

    def get_queryset(self):
        queryset = CouponUsage.objects.select_related("coupon", "order", "user").all()
        q = str(self.request.query_params.get("q", "")).strip()
        if q:
            conditions = (
                Q(coupon__code__icontains=q)
                | Q(customer_phone__icontains=q)
                | Q(user__username__icontains=q)
            )
            if q.isdigit():
                conditions = conditions | Q(order__order_number=int(q))
            queryset = queryset.filter(conditions)
        return queryset.order_by("-used_at")

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        total_discount = queryset.aggregate(total=Coalesce(Sum("discount_amount"), Decimal("0.00")))["total"]
        return Response(
            {
                "records": serializer.data,
                "summary": {
                    "records": queryset.count(),
                    "total_discount": total_discount,
                },
            }
        )


class CouponValidateView(APIView):
    permission_classes = [IsAdminOrStaff]

    def post(self, request):
        code = str(
            request.data.get("code")
            or request.data.get("coupon_code")
            or request.data.get("couponCode")
            or ""
        ).strip().upper()
        if not code:
            return error_response("Coupon code is required", 400)

        try:
            raw_order_amount = (
                request.data.get("order_amount")
                or request.data.get("amount")
                or request.data.get("total")
                or request.data.get("total_amount")
                or request.data.get("subtotal")
                or request.data.get("sub_total")
                or request.data.get("payable_amount")
            )
            order_amount = _parse_non_negative_amount(raw_order_amount)
        except ValueError as exc:
            return error_response(str(exc), 400, {"field": "order_amount"})

        try:
            coupon = Coupon.objects.get(code=code, is_active=True)
        except Coupon.DoesNotExist:
            return error_response("Invalid or inactive coupon", 404)

        now = timezone.now()
        if coupon.valid_from and coupon.valid_from > now:
            return error_response("Coupon is not active yet", 400)
        if coupon.valid_to and coupon.valid_to < now:
            return error_response("Coupon has expired", 400)
        if coupon.max_uses is not None and coupon.usage_records.count() >= coupon.max_uses:
            return error_response("Coupon max usage limit reached", 400)

        customer_phone = str(
            request.data.get("customer_phone")
            or request.data.get("customerPhone")
            or ""
        ).strip()
        if coupon.first_time_only:
            if not customer_phone:
                return error_response("Customer phone required for first-time-only coupon", 400)
            prior_paid_orders = Order.objects.filter(
                customer_phone=customer_phone,
                payment_status="PAID",
            ).exists()
            if prior_paid_orders:
                return error_response("Coupon valid only for first-time users", 400)

        discount_amount = _compute_coupon_discount(coupon, order_amount)
        if coupon.discount_type == "FREE_ITEM":
            if order_amount < (coupon.min_order_amount or Decimal("0")):
                return error_response(
                    "Order does not meet minimum amount for this coupon",
                    400,
                    {"min_order_amount": coupon.min_order_amount},
                )
            # FREE_ITEM coupon is valid even when monetary discount is zero.
            discount_amount = Decimal("0.00")
        elif discount_amount <= 0:
            if order_amount < (coupon.min_order_amount or Decimal("0")):
                return error_response(
                    "Order does not meet minimum amount for this coupon",
                    400,
                    {"min_order_amount": coupon.min_order_amount},
                )
            return error_response("Coupon is not applicable for this order", 400)

        return Response(
            {
                "id": coupon.id,
                "code": coupon.code,
                "discount_type": coupon.discount_type,
                "value": coupon.value,
                "min_order_amount": coupon.min_order_amount,
                "max_discount_amount": coupon.max_discount_amount,
                "max_uses": coupon.max_uses,
                "description": coupon.description,
                "free_item": coupon.free_item,
                "free_item_category": coupon.free_item_category,
                "first_time_only": coupon.first_time_only,
                "discount_amount": discount_amount.quantize(Decimal("0.01")),
            },
            status=200,
        )

# =====================================
# CREATE ORDER (GENERIC)
# =====================================

class OrderCreateView(generics.CreateAPIView):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [IsAdminOrStaff]


# =====================================
# TODAY ORDERS
# =====================================

class TodayOrderListView(generics.ListAPIView):

    serializer_class = KitchenOrderSerializer
    permission_classes = [IsAdminOrStaff]

    def get_queryset(self):

        today = timezone.localdate()
        start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        end = start + timedelta(days=1)

        qs = (
            Order.objects
            .filter(created_at__gte=start, created_at__lt=end)
            .select_related("table", "session", "customer")
            .prefetch_related("items__product", "items__combo", "items__addons__addon")
            .order_by("created_at")
        )

        has_explicit_filter = any([
            request_key in self.request.GET
            for request_key in ["filter", "status", "payment_status"]
        ])
        if not has_explicit_filter:
            qs = qs.exclude(status="CANCELLED")

        filtered = apply_order_filters(self.request, qs)

        # Kitchen should not see unpaid takeaway/online orders.
        # They become visible only after successful payment.
        return filtered.exclude(
            order_type__in=["TAKEAWAY", "TAKE_AWAY", "SWIGGY", "ZOMATO"],
            payment_status="UNPAID",
        )


# =====================================
# UPDATE STATUS
# =====================================

class OrderStatusUpdateView(APIView):
    permission_classes = [IsAdminOrStaff]

    def patch(self, request, pk):
        try:
            order = Order.objects.get(pk=pk)
        except Order.DoesNotExist:
            return error_response("Order not found", 404)

        next_status = (request.data.get("status") or "").strip().upper()
        if not next_status:
            return error_response("status is required", 400)

        valid_statuses = {choice[0] for choice in Order.STATUS_CHOICES}
        if next_status not in valid_statuses:
            return error_response(
                "Invalid status value",
                400,
                {"allowed_statuses": sorted(valid_statuses)},
            )

        # Unpaid takeaway/online orders can be saved as NEW (pending payment),
        # but cannot move further in kitchen workflow until payment is complete.
        if (
            (order.order_type in {"TAKEAWAY", "TAKE_AWAY", "SWIGGY", "ZOMATO"})
            and order.payment_status != "PAID"
            and next_status in {"IN_PROGRESS", "READY", "SERVED", "COMPLETED"}
        ):
            return error_response(
                "Takeaway/online order must be paid before kitchen processing",
                400,
            )

        order.status = next_status
        order.save(update_fields=["status"])
        return Response({"id": str(order.id), "status": order.status}, status=200)


class OrderCancelView(APIView):

    permission_classes = [IsAdminRole]

    def post(self, request, pk):

        try:
            order = (
                Order.objects
                .select_related("session", "table")
                .prefetch_related("items__product", "items__combo__items__product")
                .get(pk=pk)
            )
        except Order.DoesNotExist:
            return Response({"error": "Order not found"}, status=404)

        if order.status == "CANCELLED":
            return Response({"error": "Order already cancelled"}, status=400)

        log_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            # If order is already paid/completed, reverse ingredient usage before cancelling.
            if order.payment_status == "PAID" or order.status == "COMPLETED":
                order_items = list(order.items.all())
                product_ids = set()

                for item in order_items:
                    if item.product_id:
                        product_ids.add(item.product_id)
                    elif item.combo_id:
                        for combo_item in item.combo.items.all():
                            product_ids.add(combo_item.product_id)

                recipes_by_product = defaultdict(list)
                if product_ids:
                    recipes = (
                        Recipe.objects
                        .filter(product_id__in=product_ids)
                        .select_related("ingredient", "product")
                    )
                    for recipe in recipes:
                        recipes_by_product[recipe.product_id].append(recipe)

                ingredient_restore = defaultdict(Decimal)

                for item in order_items:
                    if item.product_id:
                        item_recipes = recipes_by_product.get(item.product_id, [])
                        if not item_recipes:
                            return Response(
                                {"error": f"No recipe for {item.product.name}. Cannot reverse stock."},
                                status=400,
                            )
                        for recipe in item_recipes:
                            ingredient_restore[recipe.ingredient_id] += recipe.quantity * item.quantity
                    elif item.combo_id:
                        for combo_product in item.combo.items.all():
                            item_recipes = recipes_by_product.get(combo_product.product_id, [])
                            if not item_recipes:
                                return Response(
                                    {"error": f"No recipe for {combo_product.product.name}. Cannot reverse stock."},
                                    status=400,
                                )
                            combined_qty = combo_product.quantity * item.quantity
                            for recipe in item_recipes:
                                ingredient_restore[recipe.ingredient_id] += recipe.quantity * combined_qty

                if ingredient_restore:
                    ingredients = {
                        ingredient.id: ingredient
                        for ingredient in Ingredient.objects.select_for_update().filter(id__in=ingredient_restore.keys())
                    }
                    updated_ingredients = []
                    stock_logs = []

                    for ingredient_id, restore_qty in ingredient_restore.items():
                        ingredient = ingredients.get(ingredient_id)
                        if ingredient is None:
                            return Response(
                                {"error": "Ingredient not found for stock reversal"},
                                status=400,
                            )
                        ingredient.current_stock += restore_qty
                        updated_ingredients.append(ingredient)
                        stock_logs.append(
                            StockLog(
                                ingredient=ingredient,
                                change=restore_qty,
                                reason="ADJUSTMENT",
                                user=log_user,
                            )
                        )

                    Ingredient.objects.bulk_update(updated_ingredients, ["current_stock"])
                    StockLog.objects.bulk_create(stock_logs)

                order.payment_status = "REFUNDED"

            order.status = "CANCELLED"
            if order.payment_status == "REFUNDED":
                order.save(update_fields=["status", "payment_status"])
            else:
                order.save(update_fields=["status"])

            # If this is the last non-cancelled order in session, close session and free table.
            if order.session and order.session.is_active:
                has_open_orders = (
                    order.session.orders
                    .exclude(pk=order.pk)
                    .exclude(status="CANCELLED")
                    .exists()
                )

                if not has_open_orders:
                    order.session.is_active = False
                    order.session.closed_at = timezone.now()
                    order.session.save(update_fields=["is_active", "closed_at"])

                    if order.session.table:
                        order.session.table.status = "AVAILABLE"
                        order.session.table.save(update_fields=["status"])

        return Response(
            {
                "id": str(order.id),
                "status": order.status,
                "payment_status": order.payment_status,
                "message": "Order cancelled",
            },
            status=200
        )


# =====================================
# PAYMENT
# =====================================

class OrderPaymentView(APIView):

    permission_classes = [IsAdminOrStaff]

    def post(self, request, pk): 

        method = request.data.get("method")
        reference = (request.data.get("reference") or "").strip()
        cash_received_raw = request.data.get("cash_received")

        # -------------------------
        # Validate Method
        # -------------------------
        if method not in ["CASH", "UPI", "CARD"]:
            return Response(
                {"error": "Invalid payment method"},
                status=400
            )

        # -------------------------
        # Get Order
        # -------------------------
        try:
            order = (
                Order.objects
                .select_related("table", "session")
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=OrderItem.objects.select_related("product", "combo").prefetch_related("combo__items__product"),
                    )
                )
                .get(pk=pk)
            )

        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"},
                status=404
            )

        # -------------------------
        # Check Already Paid
        # -------------------------
        if order.payment_status == "PAID":
            return Response(
                {"error": "Already paid"},
                status=400
            )

        log_user = request.user if request.user.is_authenticated else None
        try:
            with transaction.atomic():

                # -------------------------
                # Use Saved Total
                # -------------------------
                total = order.total_amount
            

            # -------------------------
            # Final Amount
            # -------------------------
                discount = order.discount_amount or Decimal("0.00")

                final_amount = total - discount

                if final_amount < 0:
                    final_amount = Decimal("0.00")

                if method == "CASH":
                    try:
                        cash_received = _parse_non_negative_amount(cash_received_raw)
                    except ValueError as exc:
                        return Response({"error": str(exc)}, status=400)
                    if cash_received < final_amount:
                        return Response(
                            {"error": "Cash received is less than payable amount"},
                            status=400,
                        )
                    reference_id = f"CASH:{cash_received:.2f}"
                elif method == "CARD":
                    if not reference:
                        return Response({"error": "Card reference/number is required"}, status=400)
                    reference_id = reference
                else:
                    # UPI extra info is optional by requirement.
                    reference_id = reference or None

            # -------------------------
            # Create Payment
            # -------------------------
                Payment.objects.create(
                    order=order,
                    method=method,
                    amount=final_amount,
                    status="SUCCESS",
                    reference_id=reference_id,
                )

            # -------------------------
            # Generate Bill Number
            # 000000000001 Format
            # -------------------------
                last_order = (
                    Order.objects
                    .filter(bill_number__isnull=False)
                    .only("bill_number")
                    .order_by("-created_at")
                    .first()
                )

                next_number = 1
                if last_order:
                    try:
                        digits = "".join(ch for ch in str(last_order.bill_number) if ch.isdigit())
                        if digits:
                            next_number = int(digits) + 1
                    except (TypeError, ValueError):
                        pass

                bill_no = format_bill_number(next_number)

            # -------------------------
            # Update Order
            # -------------------------
                order.bill_number = bill_no
                order.payment_status = "PAID"

            # Keep dine-in settlement flow unchanged.
                if order.order_type == "DINE_IN":
                    order.status = "COMPLETED"
                else:
                    # For takeaway/online partners: paid first, then kitchen workflow continues.
                    if order.status in {"CANCELLED", "COMPLETED"}:
                        return Response({"error": "Order cannot be moved to kitchen after payment"}, status=400)
                    order.status = "NEW"
                order.save(update_fields=["bill_number", "payment_status", "status"])

            # -------------------------
            # Stock Deduction
            # -------------------------
                order_items = list(order.items.all())
                product_ids = set()

                for item in order_items:
                    if item.product_id:
                        product_ids.add(item.product_id)
                    elif item.combo_id:
                        for combo_item in item.combo.items.all():
                            product_ids.add(combo_item.product_id)

                recipes_by_product = defaultdict(list)
                if product_ids:
                    recipes = (
                        Recipe.objects
                        .filter(product_id__in=product_ids)
                        .select_related("ingredient", "product")
                    )
                    for recipe in recipes:
                        recipes_by_product[recipe.product_id].append(recipe)

                ingredient_usage = defaultdict(Decimal)

                for item in order_items:
                    if item.product_id:
                        item_recipes = recipes_by_product.get(item.product_id, [])
                        if not item_recipes:
                            raise ValidationError(f"No recipe for {item.product.name}")

                        for recipe in item_recipes:
                            ingredient_usage[recipe.ingredient_id] += recipe.quantity * item.quantity

                    elif item.combo_id:
                        for combo_product in item.combo.items.all():
                            item_recipes = recipes_by_product.get(combo_product.product_id, [])
                            if not item_recipes:
                                raise ValidationError(f"No recipe for {combo_product.product.name}")

                            combined_qty = combo_product.quantity * item.quantity
                            for recipe in item_recipes:
                                ingredient_usage[recipe.ingredient_id] += recipe.quantity * combined_qty

                if ingredient_usage:
                    ingredients = {
                        ingredient.id: ingredient
                        for ingredient in Ingredient.objects.select_for_update().filter(id__in=ingredient_usage.keys())
                    }
                    updated_ingredients = []
                    stock_logs = []

                    for ingredient_id, used_qty in ingredient_usage.items():
                        ingredient = ingredients.get(ingredient_id)
                        if ingredient is None:
                            raise ValidationError("Ingredient not found for stock deduction")

                        if ingredient.current_stock < used_qty:
                            raise ValidationError(f"Not enough stock for {ingredient.name}")

                        ingredient.current_stock -= used_qty
                        updated_ingredients.append(ingredient)
                        stock_logs.append(
                            StockLog(
                                ingredient=ingredient,
                                change=-used_qty,
                                reason="SALE",
                                user=log_user
                            )
                        )

                    Ingredient.objects.bulk_update(updated_ingredients, ["current_stock"])
                    StockLog.objects.bulk_create(stock_logs)

            # -------------------------
            # Close Session + Free Table
            # -------------------------
                if order.order_type == "DINE_IN" and order.session:

                    order.session.is_active = False
                    order.session.closed_at = timezone.now()
                    order.session.save(update_fields=["is_active", "closed_at"])

                    table = order.session.table
                    table.status = "AVAILABLE"
                    table.save(update_fields=["status"])

            # Fire and forget: payment must not fail if WhatsApp send fails.
                transaction.on_commit(
                    lambda: send_order_invoice_whatsapp(
                        order=order,
                        bill_number=bill_no,
                        final_amount=f"{final_amount:.2f}",
                        method=method,
                    )
                )
        except ValidationError as exc:
            detail = getattr(exc, "detail", exc)
            if isinstance(detail, (list, tuple)) and detail:
                message = str(detail[0])
            elif isinstance(detail, dict):
                first_val = next(iter(detail.values()), "Payment failed")
                if isinstance(first_val, (list, tuple)) and first_val:
                    message = str(first_val[0])
                else:
                    message = str(first_val)
            else:
                message = str(detail)
            return Response({"error": message}, status=400)

        return Response(
            {
                "message": "Payment successful",
                "bill_number": bill_no,
                "final_amount": final_amount,
                "order_status": order.status,
                "payment_status": order.payment_status,
            },
            status=200
        )


# =====================================
# INVOICE
# =====================================

class OrderInvoiceView(APIView):

    permission_classes = [IsAdminOrStaff]

    def get(self, request, pk):

        try:
            order = (
                Order.objects
                .select_related("staff")
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=OrderItem.objects.select_related("product", "combo").prefetch_related("addons__addon"),
                    ),
                    "payments",
                    Prefetch("coupon_usages", queryset=CouponUsage.objects.select_related("coupon").order_by("-used_at")),
                )
                .get(pk=pk)
            )

        except Order.DoesNotExist:
            return Response(
                {"error": "Invoice not found"},
                status=404
            )

        if not order.bill_number:
            return Response(
                {"error": "Invoice not generated for this order yet"},
                status=400,
            )

        if order.payment_status not in {"PAID", "REFUNDED"}:
            return Response(
                {"error": "Invoice available only for paid/refunded orders"},
                status=400,
            )

        items_data = []

        subtotal = Decimal("0.00")
        total_gst = Decimal("0.00")
        grand_total = Decimal("0.00")

        payment = next((pay for pay in reversed(list(order.payments.all())) if pay.status == "SUCCESS"), None)
        coupon_usage = next(iter(order.coupon_usages.all()), None)
        coupon_discount = Decimal(str(coupon_usage.discount_amount if coupon_usage else 0))
        total_discount = Decimal(str(order.discount_amount or 0))
        if coupon_discount > total_discount:
            coupon_discount = total_discount
        manual_discount = total_discount - coupon_discount

        for item in order.items.all():

            base_total = item.base_price * item.quantity
            gst_total = item.gst_amount * item.quantity
            line_total = item.price_at_time * item.quantity

            subtotal += base_total
            total_gst += gst_total
            grand_total += line_total

            addon_counts = defaultdict(lambda: {"name": "", "unit_price": Decimal("0.00"), "quantity_per_item": 0})
            for addon_row in item.addons.all():
                if not addon_row.addon:
                    continue
                addon_key = str(addon_row.addon_id)
                addon_counts[addon_key]["name"] = addon_row.addon.name
                addon_counts[addon_key]["unit_price"] = Decimal(str(addon_row.price_at_time or 0))
                addon_counts[addon_key]["quantity_per_item"] += 1

            addon_data = []
            for addon_value in addon_counts.values():
                qty_per_item = int(addon_value["quantity_per_item"] or 0)
                qty_total = qty_per_item * item.quantity
                unit_price = Decimal(str(addon_value["unit_price"] or 0))
                addon_data.append({
                    "name": addon_value["name"],
                    "quantity_per_item": qty_per_item,
                    "quantity_total": qty_total,
                    "unit_price": unit_price,
                    "line_total": unit_price * qty_total,
                })

            items_data.append({
                "name": item.product.name if item.product else (item.combo.name if item.combo else ""),
                "quantity": item.quantity,

                "base_price": item.base_price,
                "gst_percent": item.gst_percent,
                "gst_amount": item.gst_amount,

                "line_total": line_total,   # ✅ IMPORTANT
                "addons": addon_data,
            })


        return Response({

            "bill_number": format_bill_number(order.bill_number),
            "date": order.created_at,

            "order_type": order.order_type,

            "staff": order.staff.username if order.staff else None,

            "customer_name": order.customer_name,

            "subtotal": subtotal,
            "total_gst": total_gst,

            "grand_total": grand_total,   # ✅ IMPORTANT

            "discount": order.discount_amount,
            "manual_discount": manual_discount,
            "coupon_discount": coupon_discount,
            "discount_type": "AMOUNT",
            "coupon_details": {
                "code": coupon_usage.coupon.code if coupon_usage and coupon_usage.coupon else "",
                "discount_type": coupon_usage.coupon.discount_type if coupon_usage and coupon_usage.coupon else "",
                "value": coupon_usage.coupon.value if coupon_usage and coupon_usage.coupon else Decimal("0.00"),
                "discount_amount": coupon_discount,
                "free_item": coupon_usage.coupon.free_item if coupon_usage and coupon_usage.coupon else "",
                "free_item_category": coupon_usage.coupon.free_item_category if coupon_usage and coupon_usage.coupon else "",
            } if coupon_usage and coupon_usage.coupon else None,
            "discount_breakdown": {
                "manual_discount": manual_discount,
                "coupon_discount": coupon_discount,
                "total_discount": order.discount_amount or Decimal("0.00"),
            },

            "final_amount": grand_total - (order.discount_amount or 0),

            "payment_method": payment.method if payment else None,

            "payment_status": order.payment_status,
            "line_items": items_data,
            "items": items_data
        })
# =====================================
# ADD ITEMS
# =====================================

class AddOrderItemsView(APIView):

    permission_classes = [IsAdminOrStaff]

    def post(self, request, order_id):
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return error_response("Order not found", 404)

        if order.payment_status == "PAID" or order.status in {"COMPLETED", "CANCELLED"}:
            return error_response(
                "Cannot modify items for paid/completed/cancelled order",
                400,
            )

        items = request.data.get("items", [])
        discount_amount_raw = request.data.get("discount_amount")
        discount_percent_raw = request.data.get("discount_percent")
        coupon_code_raw = request.data.get("coupon_code")
        if not isinstance(items, list):
            return error_response("items must be a list", 400)
        if not items:
            return error_response("No items provided", 400)

        total = Decimal("0.00")
        coupon_obj = None
        prepared_items = []
        product_ids = set()
        combo_ids = set()
        addon_ids = set()

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                return error_response("Each item must be an object", 400, {"item_index": idx})

            product_id = item.get("product") or item.get("productId")
            combo_id = item.get("combo") or item.get("comboId")

            if not product_id and not combo_id:
                return error_response(
                    "Product id or combo id is required",
                    400,
                    {"item_index": idx},
                )
            if product_id and combo_id:
                return error_response(
                    "Provide either product id or combo id, not both",
                    400,
                    {"item_index": idx},
                )

            try:
                qty = _parse_positive_quantity(item.get("quantity"))
            except ValueError as exc:
                return error_response(
                    str(exc),
                    400,
                    {"item_index": idx},
                )

            raw_addons = item.get("addons", [])
            if raw_addons in (None, ""):
                raw_addons = []
            if not isinstance(raw_addons, list):
                return error_response(
                    "addons must be a list",
                    400,
                    {"item_index": idx},
                )

            normalized_addons = {}
            for addon_row in raw_addons:
                addon_id = None
                addon_qty = 1
                if isinstance(addon_row, dict):
                    addon_id = addon_row.get("addon") or addon_row.get("id")
                    addon_qty_raw = addon_row.get("quantity", 1)
                    try:
                        addon_qty = _parse_positive_quantity(addon_qty_raw)
                    except ValueError as exc:
                        return error_response(
                            str(exc),
                            400,
                            {"item_index": idx, "field": "addons.quantity"},
                        )
                elif addon_row:
                    addon_id = addon_row

                if not addon_id:
                    return error_response(
                        "Invalid addon payload",
                        400,
                        {"item_index": idx},
                    )

                addon_id_str = str(addon_id)
                normalized_addons[addon_id_str] = normalized_addons.get(addon_id_str, 0) + addon_qty
                addon_ids.add(addon_id_str)

            normalized_addon_rows = sorted(normalized_addons.items(), key=lambda row: row[0])
            if combo_id and normalized_addon_rows:
                return error_response(
                    "Addons are supported only for product items",
                    400,
                    {"item_index": idx},
                )

            if product_id:
                product_ids.add(str(product_id))
            else:
                combo_ids.add(str(combo_id))

            prepared_items.append(
                {
                    "item_index": idx,
                    "product_id": str(product_id) if product_id else None,
                    "combo_id": str(combo_id) if combo_id else None,
                    "quantity": qty,
                    "addons": normalized_addon_rows,
                }
            )

        merged_items = {}
        for item in prepared_items:
            if item["product_id"]:
                addon_signature = ",".join([f"{addon_id}:{addon_qty}" for addon_id, addon_qty in item["addons"]])
                key = f"product:{item['product_id']}:{addon_signature}"
            else:
                key = f"combo:{item['combo_id']}"
            if key not in merged_items:
                merged_items[key] = {
                    "item_index": item["item_index"],
                    "product_id": item["product_id"],
                    "combo_id": item["combo_id"],
                    "quantity": 0,
                    "addons": item["addons"],
                }
            merged_items[key]["quantity"] += item["quantity"]

        products_map = {str(product.id): product for product in Product.objects.filter(id__in=product_ids)}
        combos_map = {str(combo.id): combo for combo in Combo.objects.filter(id__in=combo_ids)}
        addons_map = {str(addon.id): addon for addon in Addon.objects.filter(id__in=addon_ids)}

        prepared_order_items = []
        for item in merged_items.values():
            product = None
            combo = None
            selected_addons = []
            addon_total = Decimal("0.00")
            if item["product_id"]:
                product = products_map.get(item["product_id"])
                if not product:
                    return error_response(
                        "Invalid product id",
                        400,
                        {"item_index": item["item_index"], "product": item["product_id"]},
                    )
                for addon_id, addon_qty in item["addons"]:
                    addon_obj = addons_map.get(addon_id)
                    if not addon_obj:
                        return error_response(
                            "Invalid addon id",
                            400,
                            {"item_index": item["item_index"], "addon": addon_id},
                        )
                    selected_addons.append({"addon": addon_obj, "quantity": addon_qty})
                    addon_total += addon_obj.price * addon_qty

                base_price = product.price + addon_total
                gst_percent = product.gst_percent or Decimal("0.00")
            else:
                combo = combos_map.get(item["combo_id"])
                if not combo:
                    return error_response(
                        "Invalid combo id",
                        400,
                        {"item_index": item["item_index"], "combo": item["combo_id"]},
                    )
                base_price = combo.price
                gst_percent = combo.gst_percent or Decimal("0.00")

            taxable_base = product.price if product else base_price
            gst_amount = (taxable_base * gst_percent) / 100
            final_price = base_price + gst_amount

            prepared_order_items.append(
                {
                    "product": product,
                    "combo": combo,
                    "quantity": item["quantity"],
                    "base_price": base_price,
                    "gst_percent": gst_percent,
                    "gst_amount": gst_amount,
                    "price_at_time": final_price,
                    "addons": selected_addons,
                }
            )
            total += final_price * item["quantity"]

        coupon_discount_amount = Decimal("0.00")
        if coupon_code_raw not in (None, ""):
            coupon_code = str(coupon_code_raw).strip().upper()
            try:
                coupon_obj = Coupon.objects.get(code=coupon_code, is_active=True)
            except Coupon.DoesNotExist:
                return error_response("Invalid or inactive coupon", 400, {"field": "coupon_code"})

            now = timezone.now()
            if coupon_obj.valid_from and coupon_obj.valid_from > now:
                return error_response("Coupon is not active yet", 400, {"field": "coupon_code"})
            if coupon_obj.valid_to and coupon_obj.valid_to < now:
                return error_response("Coupon has expired", 400, {"field": "coupon_code"})
            if coupon_obj.max_uses is not None and coupon_obj.usage_records.count() >= coupon_obj.max_uses:
                return error_response("Coupon max usage limit reached", 400, {"field": "coupon_code"})
            if coupon_obj.first_time_only:
                customer_phone = str(order.customer_phone or "").strip()
                if not customer_phone:
                    return error_response(
                        "Customer phone required for first-time-only coupon",
                        400,
                        {"field": "coupon_code"},
                    )
                prior_paid_orders = Order.objects.filter(
                    customer_phone=customer_phone,
                    payment_status="PAID",
                ).exclude(id=order.id).exists()
                if prior_paid_orders:
                    return error_response("Coupon valid only for first-time users", 400, {"field": "coupon_code"})

            coupon_discount_amount = _compute_coupon_discount(coupon_obj, total)
            if coupon_obj.discount_type == "FREE_ITEM":
                if total < (coupon_obj.min_order_amount or Decimal("0")):
                    return error_response(
                        "Order does not meet minimum amount for this coupon",
                        400,
                        {"field": "coupon_code", "min_order_amount": coupon_obj.min_order_amount},
                    )
                coupon_discount_amount = Decimal("0.00")
            elif coupon_discount_amount <= 0:
                if total < (coupon_obj.min_order_amount or Decimal("0")):
                    return error_response(
                        "Order does not meet minimum amount for this coupon",
                        400,
                        {"field": "coupon_code", "min_order_amount": coupon_obj.min_order_amount},
                    )
                return error_response("Coupon is not applicable for this order", 400, {"field": "coupon_code"})

        has_discount_amount = discount_amount_raw not in (None, "")
        has_discount_percent = discount_percent_raw not in (None, "")
        if has_discount_amount and has_discount_percent:
            return error_response("Provide either discount_amount or discount_percent, not both", 400)

        discount_amount = Decimal("0.00")
        if has_discount_amount:
            try:
                discount_amount = _parse_non_negative_amount(discount_amount_raw)
            except ValueError as exc:
                return error_response(str(exc), 400, {"field": "discount_amount"})
        elif has_discount_percent:
            try:
                discount_percent = _parse_non_negative_amount(discount_percent_raw)
            except ValueError as exc:
                return error_response(str(exc), 400, {"field": "discount_percent"})
            if discount_percent > Decimal("100"):
                return error_response("discount_percent cannot exceed 100", 400)
            discount_amount = (total * discount_percent) / Decimal("100")

        if discount_amount > total:
            return error_response("discount_amount cannot exceed order total", 400)

        total_discount_amount = discount_amount + coupon_discount_amount
        if total_discount_amount > total:
            return error_response("Total discount cannot exceed order total", 400)

        with transaction.atomic():
            order.items.all().delete()
            for item in prepared_order_items:
                order_item = OrderItem.objects.create(
                    order=order,
                    product=item["product"],
                    combo=item["combo"],
                    quantity=item["quantity"],
                    base_price=item["base_price"],
                    gst_percent=item["gst_percent"],
                    gst_amount=item["gst_amount"],
                    price_at_time=item["price_at_time"],
                )
                if item["addons"]:
                    addon_rows = []
                    for addon_data in item["addons"]:
                        addon_obj = addon_data["addon"]
                        addon_qty = addon_data["quantity"]
                        for _ in range(addon_qty):
                            addon_rows.append(
                                OrderItemAddon(
                                    order_item=order_item,
                                    addon=addon_obj,
                                    price_at_time=addon_obj.price,
                                )
                            )
                    if addon_rows:
                        OrderItemAddon.objects.bulk_create(addon_rows)

            order.total_amount = total
            order.discount_amount = total_discount_amount
            order.save(update_fields=["total_amount", "discount_amount"])

            if coupon_obj:
                CouponUsage.objects.update_or_create(
                    coupon=coupon_obj,
                    order=order,
                    defaults={
                        "user": request.user if request.user.is_authenticated else None,
                        "customer_phone": str(order.customer_phone or ""),
                        "discount_amount": coupon_discount_amount,
                    },
                )
            else:
                order.coupon_usages.all().delete()

        return Response(
            {
                "message": "Items added successfully",
                "subtotal_with_gst": total,
                "manual_discount_amount": discount_amount,
                "coupon_discount_amount": coupon_discount_amount,
                "discount_amount": total_discount_amount,
                "payable_amount": total - total_discount_amount,
                "items_count": len(prepared_order_items),
            },
            status=200
        )


# =====================================
# CREATE ORDER (CUSTOM)
# =====================================

class OrderCreateView(APIView):

    permission_classes = [IsAdminOrStaff]

    def post(self, request):

        raw_order_type = request.data.get("order_type", "DINE_IN")
        order_type = (raw_order_type or "DINE_IN").strip().upper()
        if order_type == "TAKE_AWAY":
            order_type = "TAKEAWAY"

        table_id = request.data.get("table")
        session_id = request.data.get("session")

        customer_name = request.data.get("customer_name")
        customer_phone = request.data.get("customer_phone")

        session = None
        customer = None

        # -------------------------
        # DINE IN FLOW
        # -------------------------
        if order_type == "DINE_IN":

            if not session_id:
                return error_response("Session required for dine-in", 400)

            try:
                session = TableSession.objects.get(id=session_id)
            except TableSession.DoesNotExist:
                return error_response("Invalid session", 400)

            # Get / Create customer from session
            if session.customer_phone:

                customer, _ = Customer.objects.get_or_create(
                    phone=session.customer_phone,
                    defaults={
                        "name": session.customer_name
                    }
                )

                customer_name = session.customer_name
                customer_phone = session.customer_phone


        # -------------------------
        # TAKEAWAY FLOW
        # -------------------------
        elif order_type == "TAKEAWAY":

            if not customer_name or not customer_phone:
                return Response(
                    {
                        "error": "Customer name and phone required for takeaway"
                    },
                    status=400
                )

            # Save / Get customer
            customer, _ = Customer.objects.get_or_create(
                phone=customer_phone,
                defaults={
                    "name": customer_name
                }
            )

        # -------------------------
        # SWIGGY / ZOMATO FLOW
        # -------------------------
        elif order_type in {"SWIGGY", "ZOMATO"}:
            customer_name = customer_name or order_type.title()
            if customer_phone:
                customer, _ = Customer.objects.get_or_create(
                    phone=customer_phone,
                    defaults={"name": customer_name},
                )

        else:
            return error_response("Invalid order type", 400)


        # -------------------------
        # CREATE ORDER
        # -------------------------
        resolved_table_id = None
        if order_type == "DINE_IN":
            if session and session.table_id:
                resolved_table_id = session.table_id
            else:
                resolved_table_id = table_id

        order = Order.objects.create(

            order_type=order_type,

            table_id=resolved_table_id if order_type == "DINE_IN" else None,

            session=session if order_type == "DINE_IN" else None,

            customer=customer,

            # ✅ Always store directly
            customer_name=customer_name,
            customer_phone=customer_phone,

            staff=request.user
        )

        return Response(
            {
                "id": order.id,
                "order_id": format_order_id(order.order_number)
            },
            status=201
        )

class OrderListView(generics.ListAPIView):

    permission_classes = [IsAdminOrStaff]
    serializer_class = OrderListSerializer

    def get_queryset(self):

        qs = (
            Order.objects
            .select_related("table", "customer")
            .annotate(items_count=Count("items"))
            .order_by("-created_at")
        )
        return apply_order_filters(self.request, qs)


class RecentOrderListView(APIView):

    permission_classes = [IsAdminOrStaff]

    def get(self, request):

        try:
            limit = int(request.GET.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10

        limit = max(1, min(limit, 100))

        qs = (
            Order.objects
            .select_related("table", "customer")
            .annotate(items_count=Count("items"))
            .order_by("-created_at")
        )

        if (getattr(request.user, "role", "") or "").upper() == "STAFF":
            qs = qs.filter(staff=request.user)

        qs = apply_order_filters(request, qs)
        orders = qs[:limit]

        data = []
        for order in orders:
            customer_name = order.customer_name
            if not customer_name and order.customer:
                customer_name = order.customer.name

            if order.payment_status == "PAID":
                display_status = "PAID"
            elif order.payment_status == "REFUNDED":
                display_status = "REFUNDED"
            elif order.status == "CANCELLED":
                display_status = "CANCELLED"
            else:
                display_status = "PENDING"

            data.append({
                "id": str(order.id),
                "order_id": format_order_id(order.order_number),
                "bill_number": format_bill_number(order.bill_number),
                "customer_name": customer_name,
                "table_name": order.table.number if order.table else None,
                "items_count": order.items_count,
                "total_amount": order.total_amount,
                "order_type": order.order_type,
                "status": display_status,
                "order_status": order.status,
                "payment_status": order.payment_status,
                "created_at": order.created_at
            })

        return Response(data)


@csrf_exempt
def send_whatsapp(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        phone = data.get("phone")
        vars_list = data.get("variables") or []
        if not isinstance(vars_list, list):
            return JsonResponse({"error": "variables must be a list"}, status=400)

        template_id = data.get("message_id")
        variables_values = "|".join(str(v) for v in vars_list)
        result = send_fast2sms_whatsapp_message(
            phone=phone,
            variables_values=variables_values,
            message_id=template_id,
        )

        status_code = 200 if result.get("sent") else 400
        return JsonResponse(result, status=status_code)

    return JsonResponse({"error": "Invalid request"}, status=405)

    # =====================================
# ORDER DETAIL (FOR POS)
# =====================================

class OrderDetailView(generics.RetrieveAPIView):

    queryset = Order.objects.select_related(
        "table",
        "session",
        "customer"
    ).prefetch_related(
        "items__product",
        "items__combo",
        "items__addons__addon",
    )

    serializer_class = OrderDetailSerializer

    permission_classes = [IsAdminOrStaff]

