from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, F, Max, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, ExtractHour, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import StaffSessionLog
from accounts.permissions import IsAdminOrStaff, IsAdminRole
from inventory.models import Ingredient, PurchaseInvoice, PurchaseItem, StockLog
from orders.models import CouponUsage, Order, OrderItem
from orders.serializers import CouponUsageSerializer
from orders.utils import format_bill_number, format_order_id
from payments.models import Payment
from products.models import ComboItem, Product, Recipe


def _as_date(value):
    return parse_date(value) if value else None


def _resolve_date_range(request):
    date_param = _as_date(request.GET.get("date"))
    from_date = _as_date(request.GET.get("from_date")) or _as_date(request.GET.get("start"))
    to_date = _as_date(request.GET.get("to_date")) or _as_date(request.GET.get("end"))
    if date_param:
        return date_param, date_param
    return from_date, to_date


def _filters(request):
    return {
        "order_type": (request.GET.get("order_type") or "").strip().upper(),
        "staff": (request.GET.get("staff") or "").strip(),
        "payment_method": (request.GET.get("payment_method") or "").strip().upper(),
        "supplier": (request.GET.get("supplier") or "").strip(),
        "category": (request.GET.get("category") or "").strip(),
    }


def _apply_date(qs, field, start, end):
    if start and end:
        return qs.filter(**{f"{field}__range": [start, end]})
    if start:
        return qs.filter(**{f"{field}__gte": start})
    if end:
        return qs.filter(**{f"{field}__lte": end})
    return qs


def _order_qs(request, statuses=None):
    start, end = _resolve_date_range(request)
    flt = _filters(request)
    qs = Order.objects.all()
    if statuses:
        qs = qs.filter(status__in=statuses)
    qs = _apply_date(qs, "created_at__date", start, end)
    if flt["order_type"]:
        qs = qs.filter(order_type=flt["order_type"])
    if flt["staff"]:
        qs = qs.filter(Q(staff__username__iexact=flt["staff"]) | Q(staff_id=flt["staff"]))
    if flt["payment_method"]:
        qs = qs.filter(payments__status="SUCCESS", payments__method=flt["payment_method"])
    if flt["category"]:
        qs = qs.filter(
            Q(items__product__category__name__icontains=flt["category"])
            | Q(items__product__category_id=flt["category"])
        )
    if (getattr(request.user, "role", "") or "").upper() == "STAFF":
        qs = qs.filter(staff=request.user)
    return qs.distinct()


def _settled_order_qs(request):
    return _order_qs(request).exclude(status="CANCELLED").filter(
        Q(payment_status="PAID") | Q(status="COMPLETED")
    )


def _meta(request, report_name):
    today = timezone.localdate().isoformat()
    from_date, to_date = _resolve_date_range(request)
    return {
        "company_name": "Kensei Food & Beverages Private Limited",
        "gst_no": "33AACCA8432H1ZZ",
        "address": "DIP & DASH PERUNGUDI CHENNAI",
        "report_name": report_name,
        "from_date": str(from_date or today),
        "to_date": str(to_date or today),
        "generated_on": timezone.localtime().isoformat(),
        "generated_by": request.user.username if request.user.is_authenticated else "System",
    }


def _payload(request, report_name, summary=None, data=None, product_breakdown=None):
    return Response(
        {
            "meta": _meta(request, report_name),
            "summary": summary or [],
            "data": data or [],
            "product_breakdown": product_breakdown or [],
        }
    )


def _report_daily_sales(request):
    qs = _settled_order_qs(request)
    items = OrderItem.objects.filter(order__in=qs)
    pay_rows = Payment.objects.filter(order__in=qs, status="SUCCESS").values("method").annotate(total=Coalesce(Sum("amount"), Decimal("0.00")))
    pay_map = {r["method"]: r["total"] for r in pay_rows}
    sum_row = qs.aggregate(
        total_orders=Count("id"),
        net_sales=Coalesce(Sum("total_amount"), Decimal("0.00")),
        discount=Coalesce(Sum("discount_amount"), Decimal("0.00")),
    )
    qty = items.aggregate(v=Coalesce(Sum("quantity"), 0))["v"] or 0
    gst = items.aggregate(v=Coalesce(Sum(F("gst_amount") * F("quantity")), Decimal("0.00")))["v"] or Decimal("0.00")
    gross = (sum_row["net_sales"] or Decimal("0.00")) + (sum_row["discount"] or Decimal("0.00"))

    summary = [{
        "from_date": _meta(request, "Daily Sales Report")["from_date"],
        "to_date": _meta(request, "Daily Sales Report")["to_date"],
        "total_orders": sum_row["total_orders"] or 0,
        "total_items_sold": qty,
        "gross_sales": gross,
        "total_discount": sum_row["discount"] or Decimal("0.00"),
        "total_gst": gst,
        "net_sales": sum_row["net_sales"] or Decimal("0.00"),
        "cash_total": pay_map.get("CASH", Decimal("0.00")),
        "upi_total": pay_map.get("UPI", Decimal("0.00")),
        "card_total": pay_map.get("CARD", Decimal("0.00")),
    }]
    data = list(qs.annotate(day=TruncDate("created_at")).values("day").annotate(order_count=Count("id"), amount=Coalesce(Sum("total_amount"), Decimal("0.00"))).order_by("day"))
    for r in data:
        r["date"] = str(r.pop("day"))
    pb = list(
        items.filter(product__isnull=False)
        .values("product__name", "product__category__name")
        .annotate(
            quantity_sold=Coalesce(Sum("quantity"), 0),
            gross_amount=Coalesce(Sum(F("quantity") * F("price_at_time")), Decimal("0.00")),
            gst=Coalesce(Sum(F("quantity") * F("gst_amount")), Decimal("0.00")),
        )
        .order_by("-quantity_sold")
    )
    ordered_pb = []
    for r in pb:
        gross_amount = r.get("gross_amount") or Decimal("0.00")
        discount = Decimal("0.00")
        gst_amount = r.get("gst") or Decimal("0.00")
        ordered_pb.append({
            "product_name": r.get("product__name") or "Unknown",
            "category": r.get("product__category__name") or "-",
            "quantity_sold": r.get("quantity_sold") or 0,
            "gross_amount": gross_amount,
            "discount": discount,
            "gst": gst_amount,
            "net_amount": gross_amount - discount,
        })
    return summary, data, ordered_pb


def _report_product_wise(request):
    qs = _settled_order_qs(request)
    rows = (
        OrderItem.objects.filter(order__in=qs, product__isnull=False)
        .values("product_id", "product__name", "product__category__name", "product__image")
        .annotate(quantity_sold=Coalesce(Sum("quantity"), 0), total_revenue=Coalesce(Sum(F("quantity") * F("price_at_time")), Decimal("0.00")))
        .order_by("-quantity_sold")
    )
    data = []
    for r in rows:
        qty = r["quantity_sold"] or 0
        revenue = r["total_revenue"] or Decimal("0.00")
        profit = revenue
        data.append({
            "product_name": r["product__name"] or "Unknown",
            "sku_code": str(r["product_id"])[:8] if r["product_id"] else "-",
            "category": r["product__category__name"] or "-",
            "image_url": request.build_absolute_uri(str(r["product__image"])) if r.get("product__image") else "",
            "quantity_sold": qty,
            "total_revenue": revenue,
            "total_discount": Decimal("0.00"),
            "net_revenue": revenue,
            "avg_selling_price": (revenue / qty) if qty else Decimal("0.00"),
            "profit": profit,
            "profit_margin_percent": Decimal("100.00") if revenue else Decimal("0.00"),
        })
    summary = [{"products": len(data), "net_revenue": sum((r["net_revenue"] for r in data), Decimal("0.00"))}]
    return summary, data, []


def _report_payment_method(request):
    qs = _order_qs(request)
    payments = Payment.objects.filter(order__in=qs)
    start, end = _resolve_date_range(request)
    payments = _apply_date(payments, "paid_at__date", start, end)
    rows = (
        payments.annotate(day=TruncDate("paid_at")).values("day").annotate(
            order_count=Count("order", distinct=True),
            cash_total=Coalesce(Sum("amount", filter=Q(status="SUCCESS", method="CASH")), Decimal("0.00")),
            upi_total=Coalesce(Sum("amount", filter=Q(status="SUCCESS", method="UPI")), Decimal("0.00")),
            card_total=Coalesce(Sum("amount", filter=Q(status="SUCCESS", method="CARD")), Decimal("0.00")),
            refund_amount=Coalesce(Sum("amount", filter=Q(status="REFUNDED")), Decimal("0.00")),
        ).order_by("day")
    )
    data = []
    for r in rows:
        machine = Decimal("0.00")
        net = (r["cash_total"] + r["upi_total"] + r["card_total"]) - machine - r["refund_amount"]
        data.append({
            "date": str(r["day"]),
            "order_count": r["order_count"],
            "cash_total": r["cash_total"],
            "upi_total": r["upi_total"],
            "card_total": r["card_total"],
            "machine_charges": machine,
            "net_received": net,
            "refund_amount": r["refund_amount"],
        })
    return [{"days": len(data)}], data, []


def _report_discount(request):
    qs = _settled_order_qs(request).filter(discount_amount__gt=0).select_related("customer", "staff")
    data = []
    for o in qs.order_by("-created_at"):
        gross = (o.total_amount or Decimal("0.00")) + (o.discount_amount or Decimal("0.00"))
        pct = ((o.discount_amount / gross) * 100) if gross else Decimal("0.00")
        data.append({
            "date": str(timezone.localtime(o.created_at).date()),
            "order_no": format_order_id(o.order_number) or str(o.id)[:8],
            "customer_name": (o.customer.name if o.customer else None) or o.customer_name or "Walk-in",
            "discount_type": "Flat Amount",
            "discount_percent": pct,
            "discount_amount": o.discount_amount,
            "applied_by": o.staff.username if o.staff else "-",
            "order_value": gross,
            "final_amount": o.total_amount,
        })
    return [{"rows": len(data)}], data, []


def _report_cancelled(request):
    qs = _order_qs(request, ["CANCELLED"]).select_related("customer", "staff")
    data = [{
        "date": str(timezone.localtime(o.created_at).date()),
        "order_no": format_order_id(o.order_number) or str(o.id)[:8],
        "order_type": o.order_type,
        "customer_name": (o.customer.name if o.customer else None) or o.customer_name or "Walk-in",
        "cancelled_by": o.staff.username if o.staff else "-",
        "reason": "-",
        "order_amount": o.total_amount,
        "time_cancelled": timezone.localtime(o.created_at).strftime("%H:%M:%S"),
    } for o in qs.order_by("-created_at")]
    return [{"rows": len(data)}], data, []


def _report_kot(request):
    qs = _order_qs(request).exclude(status="CANCELLED").select_related("table")
    items = OrderItem.objects.filter(order__in=qs).select_related("order", "order__table", "product", "combo").order_by("-order__created_at")
    data = []
    for item in items:
        order = item.order
        order_no = format_order_id(order.order_number) or str(order.id)[:8]
        data.append({
            "kot_no": f"KOT-{order_no}",
            "order_no": order_no,
            "table_no": order.table.number if order.table else "-",
            "order_time": timezone.localtime(order.created_at).strftime("%H:%M:%S"),
            "item_name": (item.product.name if item.product else None) or (item.combo.name if item.combo else None) or "Unknown",
            "quantity": item.quantity,
            "status": order.status,
            "prepared_time": "-",
            "served_time": "-",
        })
    return [{"rows": len(data)}], data, []


def _report_customer(request):
    qs = _settled_order_qs(request).select_related("customer")
    grouped = {}

    for o in qs:
        customer_name = (o.customer.name if o.customer else None) or o.customer_name or "Walk-in"
        phone = (o.customer.phone if o.customer else None) or o.customer_phone or "-"
        key = (customer_name.strip().lower(), phone.strip())
        if key not in grouped:
            grouped[key] = {
                "customer_name": customer_name,
                "phone": phone,
                "total_orders": 0,
                "total_spent": Decimal("0.00"),
                "first_visit": o.created_at,
                "last_visit": o.created_at,
                "order_type_counts": defaultdict(int),
            }

        row = grouped[key]
        row["total_orders"] += 1
        row["total_spent"] += (o.total_amount or Decimal("0.00"))
        if o.created_at < row["first_visit"]:
            row["first_visit"] = o.created_at
        if o.created_at > row["last_visit"]:
            row["last_visit"] = o.created_at
        row["order_type_counts"][o.order_type] += 1

    data = []
    for row in grouped.values():
        preferred_order_type = "-"
        if row["order_type_counts"]:
            preferred_order_type = sorted(
                row["order_type_counts"].items(),
                key=lambda item: (-item[1], item[0]),
            )[0][0]

        total_orders = row["total_orders"] or 0
        total_spent = row["total_spent"] or Decimal("0.00")
        data.append({
            "customer_name": row["customer_name"],
            "phone": row["phone"],
            "total_orders": total_orders,
            "total_spent": total_spent,
            "avg_order_value": (total_spent / total_orders) if total_orders else Decimal("0.00"),
            "first_visit": timezone.localtime(row["first_visit"]).date().isoformat(),
            "last_visit": timezone.localtime(row["last_visit"]).date().isoformat(),
            "preferred_order_type": preferred_order_type,
        })

    data.sort(key=lambda r: (-(r["total_spent"] or Decimal("0.00")), r["customer_name"]))
    return [{"rows": len(data)}], data, []


def _report_peak_time(request):
    qs = _settled_order_qs(request)
    rows = qs.annotate(hr=ExtractHour("created_at")).values("hr").annotate(
        orders_count=Count("id"),
        items_sold=Coalesce(Sum("items__quantity"), 0),
        revenue=Coalesce(Sum("total_amount"), Decimal("0.00")),
    ).order_by("hr")
    data = []
    for r in rows:
        if r["hr"] is None:
            continue
        data.append({
            "time_slot_hour": f"{int(r['hr']):02d}:00",
            "orders_count": r["orders_count"],
            "items_sold": r["items_sold"],
            "revenue": r["revenue"],
            "avg_order_value": (r["revenue"] / r["orders_count"]) if r["orders_count"] else Decimal("0.00"),
        })
    return [{"slots": len(data)}], data, []


def _report_purchase(request):
    start, end = _resolve_date_range(request)
    flt = _filters(request)
    invoices = _apply_date(PurchaseInvoice.objects.select_related("vendor", "purchased_by"), "created_at__date", start, end)
    if flt["supplier"]:
        invoices = invoices.filter(Q(vendor__name__icontains=flt["supplier"]) | Q(vendor__id=flt["supplier"]))
    data = []
    for item in PurchaseItem.objects.filter(invoice__in=invoices).select_related("invoice", "invoice__vendor", "ingredient"):
        total = (item.quantity or Decimal("0")) * (item.unit_price or Decimal("0"))
        data.append({
            "purchase_date": str(timezone.localtime(item.invoice.created_at).date()),
            "purchase_id": str(item.invoice.id)[:8],
            "supplier_name": item.invoice.vendor.name if item.invoice.vendor else "-",
            "invoice_no": item.invoice.invoice_number,
            "product_name": item.ingredient.name,
            "quantity": item.quantity,
            "unit_cost": item.unit_price,
            "gst": Decimal("0.00"),
            "total_amount": total,
            "payment_status": "PAID",
        })
    return [{"rows": len(data)}], data, []


def _report_supplier_wise(request):
    start, end = _resolve_date_range(request)
    flt = _filters(request)
    invoices = _apply_date(PurchaseInvoice.objects.select_related("vendor"), "created_at__date", start, end)
    if flt["supplier"]:
        invoices = invoices.filter(Q(vendor__name__icontains=flt["supplier"]) | Q(vendor__id=flt["supplier"]))

    invoice_totals = {
        str(r["invoice_id"]): (r["total_amount"] or Decimal("0.00"))
        for r in PurchaseItem.objects.filter(invoice__in=invoices)
        .values("invoice_id")
        .annotate(total_amount=Coalesce(Sum(F("quantity") * F("unit_price")), Decimal("0.00")))
    }

    grouped = {}
    for inv in invoices:
        supplier_name = inv.vendor.name if inv.vendor else "-"
        key = str(inv.vendor_id) if inv.vendor_id else f"unknown:{supplier_name}"
        if key not in grouped:
            grouped[key] = {
                "supplier_name": supplier_name,
                "total_purchases": 0,
                "total_amount": Decimal("0.00"),
                "paid_amount": Decimal("0.00"),
                "outstanding": Decimal("0.00"),
                "last_purchase_date": inv.created_at,
            }

        row = grouped[key]
        inv_total = invoice_totals.get(str(inv.id), Decimal("0.00"))
        row["total_purchases"] += 1
        row["total_amount"] += inv_total
        row["paid_amount"] += inv_total
        row["outstanding"] = row["total_amount"] - row["paid_amount"]
        if inv.created_at > row["last_purchase_date"]:
            row["last_purchase_date"] = inv.created_at

    data = [{
        "supplier_name": row["supplier_name"],
        "total_purchases": row["total_purchases"],
        "total_amount": row["total_amount"],
        "paid_amount": row["paid_amount"],
        "outstanding": row["outstanding"],
        "last_purchase_date": timezone.localtime(row["last_purchase_date"]).date().isoformat(),
    } for row in grouped.values()]
    data.sort(key=lambda r: (-(r["total_amount"] or Decimal("0.00")), r["supplier_name"]))
    return [{"rows": len(data)}], data, []


def _report_stock(request):
    start, end = _resolve_date_range(request)
    start = start or timezone.localdate()
    end = end or timezone.localdate()
    ingredients = Ingredient.objects.order_by("name")
    rng = StockLog.objects.filter(created_at__date__range=[start, end]).values("ingredient_id").annotate(
        stock_in=Coalesce(Sum("change", filter=Q(change__gt=0)), Decimal("0.000")),
        stock_out=Coalesce(Sum(-F("change"), filter=Q(change__lt=0)), Decimal("0.000")),
        net=Coalesce(Sum("change"), Decimal("0.000")),
    )
    rng_map = {str(r["ingredient_id"]): r for r in rng}
    future = (
        StockLog.objects.filter(created_at__date__gt=end)
        .values("ingredient_id")
        .annotate(net=Coalesce(Sum("change"), Decimal("0.000")))
    )
    future_map = {str(r["ingredient_id"]): r["net"] for r in future}
    data = []
    for ing in ingredients:
        key = str(ing.id)
        row = rng_map.get(key, {})
        current_stock = ing.current_stock or Decimal("0.000")
        net_after_end = future_map.get(key, Decimal("0.000"))

        # Anchor to current stock so reports stay correct even when some
        # historical opening/manual adjustments were saved without StockLog rows.
        closing = current_stock - net_after_end
        opening = closing - row.get("net", Decimal("0.000"))
        data.append({
            "product_name": ing.name,
            "opening_stock": opening,
            "stock_in": row.get("stock_in", Decimal("0.000")),
            "stock_out": row.get("stock_out", Decimal("0.000")),
            "closing_stock": closing,
            "unit": ing.unit,
            "stock_value": closing * (ing.unit_price or Decimal("0.00")),
        })
    return [{"rows": len(data)}], data, []


def _report_stock_consumption(request):
    qs = _settled_order_qs(request)
    items = list(OrderItem.objects.filter(order__in=qs).select_related("order", "product", "combo"))
    combo_ids = {item.combo_id for item in items if item.combo_id}
    combo_items = list(ComboItem.objects.filter(combo_id__in=combo_ids).select_related("product"))

    combo_products_by_combo = defaultdict(list)
    for combo_item in combo_items:
        combo_products_by_combo[combo_item.combo_id].append(combo_item)

    product_ids = {item.product_id for item in items if item.product_id}
    product_ids.update(combo_item.product_id for combo_item in combo_items if combo_item.product_id)
    recipes = list(Recipe.objects.filter(product_id__in=product_ids).select_related("ingredient", "product"))

    recipes_by_product = defaultdict(list)
    for recipe in recipes:
        recipes_by_product[recipe.product_id].append(recipe)

    grouped = {}
    for item in items:
        if item.product_id:
            product_recipes = recipes_by_product.get(item.product_id, [])
            for recipe in product_recipes:
                ingredient = recipe.ingredient
                key = str(ingredient.id)
                if key not in grouped:
                    grouped[key] = {
                        "ingredient_name": ingredient.name,
                        "used_quantity": Decimal("0.000"),
                        "unit": ingredient.unit,
                        "related_products": set(),
                        "order_ids": set(),
                    }
                grouped[key]["used_quantity"] += Decimal(str(recipe.quantity)) * Decimal(str(item.quantity))
                grouped[key]["related_products"].add(recipe.product.name)
                grouped[key]["order_ids"].add(str(item.order_id))
            continue

        if item.combo_id:
            for combo_product in combo_products_by_combo.get(item.combo_id, []):
                product_recipes = recipes_by_product.get(combo_product.product_id, [])
                combined_qty = Decimal(str(item.quantity)) * Decimal(str(combo_product.quantity))
                for recipe in product_recipes:
                    ingredient = recipe.ingredient
                    key = str(ingredient.id)
                    if key not in grouped:
                        grouped[key] = {
                            "ingredient_name": ingredient.name,
                            "used_quantity": Decimal("0.000"),
                            "unit": ingredient.unit,
                            "related_products": set(),
                            "order_ids": set(),
                        }
                    grouped[key]["used_quantity"] += Decimal(str(recipe.quantity)) * combined_qty
                    grouped[key]["related_products"].add(recipe.product.name)
                    grouped[key]["order_ids"].add(str(item.order_id))

    start, end = _resolve_date_range(request)
    today = timezone.localdate()
    from_str = str(start or today)
    to_str = str(end or today)
    date_range = f"{from_str} to {to_str}"

    data = [{
        "ingredient_name": row["ingredient_name"],
        "used_quantity": row["used_quantity"],
        "unit": row["unit"] or "-",
        "related_product": ", ".join(sorted(row["related_products"])) if row["related_products"] else "-",
        "total_orders": len(row["order_ids"]),
        "date_range": date_range,
    } for row in grouped.values()]
    data.sort(key=lambda r: (-(r["used_quantity"] or Decimal("0.000")), r["ingredient_name"]))
    return [{"rows": len(data)}], data, []


def _report_wastage(request):
    start, end = _resolve_date_range(request)
    logs = StockLog.objects.filter(change__lt=0, reason__in=["ADJUSTMENT", "MANUAL"]).select_related("ingredient", "user")
    logs = _apply_date(logs, "created_at__date", start, end).order_by("-created_at")

    data = [{
        "ingredient_name": log.ingredient.name if log.ingredient else "-",
        "quantity_wasted": -log.change,
        "unit": log.ingredient.unit if log.ingredient else "-",
        "reason": log.get_reason_display() if hasattr(log, "get_reason_display") else log.reason,
        "staff_name": log.user.username if log.user else "-",
        "date": timezone.localtime(log.created_at).date().isoformat(),
    } for log in logs]
    return [{"rows": len(data)}], data, []


def _report_low_stock(request):
    start, end = _resolve_date_range(request)
    flt = _filters(request)

    latest_purchase = PurchaseItem.objects.filter(ingredient=OuterRef("pk"))
    if start and end:
        latest_purchase = latest_purchase.filter(invoice__created_at__date__range=[start, end])
    elif start:
        latest_purchase = latest_purchase.filter(invoice__created_at__date__gte=start)
    elif end:
        latest_purchase = latest_purchase.filter(invoice__created_at__date__lte=end)
    latest_purchase = latest_purchase.order_by("-invoice__created_at")

    ingredients = (
        Ingredient.objects.filter(current_stock__lt=F("min_stock"))
        .annotate(
            supplier_name=Subquery(latest_purchase.values("invoice__vendor__name")[:1]),
            last_purchase_at=Subquery(latest_purchase.values("invoice__created_at")[:1]),
        )
        .order_by("name")
    )

    data = []
    for ing in ingredients:
        supplier_name = ing.supplier_name or "-"
        if flt["supplier"] and flt["supplier"].lower() not in supplier_name.lower():
            continue
        required_qty = (ing.min_stock or Decimal("0")) - (ing.current_stock or Decimal("0"))
        data.append({
            "ingredient_name": ing.name,
            "current_stock": ing.current_stock,
            "reorder_level": ing.min_stock,
            "required_quantity": required_qty if required_qty > 0 else Decimal("0"),
            "supplier": supplier_name,
            "last_purchase_date": timezone.localtime(ing.last_purchase_at).date().isoformat() if ing.last_purchase_at else "-",
        })

    return [{"rows": len(data)}], data, []


def _report_ingredient(request):
    flt = _filters(request)
    latest_purchase = PurchaseItem.objects.filter(ingredient=OuterRef("pk")).order_by("-invoice__created_at")
    ingredients = Ingredient.objects.annotate(
        cost_per_unit=Coalesce(F("unit_price"), Value(Decimal("0.00"))),
        supplier_name=Coalesce(Subquery(latest_purchase.values("invoice__vendor__name")[:1]), Value("-")),
        category_name=Coalesce(Subquery(latest_purchase.values("invoice__vendor__category")[:1]), Value("-")),
    ).order_by("name")

    if flt["supplier"]:
        ingredients = ingredients.filter(supplier_name__icontains=flt["supplier"])
    if flt["category"]:
        ingredients = ingredients.filter(category_name__icontains=flt["category"])

    data = []
    for ing in ingredients:
        cost_per_unit = ing.cost_per_unit or Decimal("0.00")
        current_stock = ing.current_stock or Decimal("0.000")
        data.append({
            "ingredient_name": ing.name,
            "category": ing.category_name or "-",
            "unit": ing.unit or "-",
            "current_stock": current_stock,
            "cost_per_unit": cost_per_unit,
            "total_value": current_stock * cost_per_unit,
            "supplier": ing.supplier_name or "-",
        })
    return [{"rows": len(data)}], data, []


def _report_menu(request):
    flt = _filters(request)
    ingredient_cost_map = {
        str(r["id"]): (r["unit_price"] or Decimal("0.00"))
        for r in Ingredient.objects.values("id", "unit_price")
    }

    products = Product.objects.select_related("category").prefetch_related("recipes__ingredient").order_by("name")
    if flt["category"]:
        products = products.filter(category__name__icontains=flt["category"])

    data = []
    for product in products:
        cost_price = Decimal("0.00")
        for recipe in product.recipes.all():
            ingredient_cost = ingredient_cost_map.get(str(recipe.ingredient_id), Decimal("0.00"))
            cost_price += Decimal(str(recipe.quantity or 0)) * ingredient_cost

        price = product.price or Decimal("0.00")
        profit = price - cost_price
        profit_pct = ((profit / price) * 100) if price else Decimal("0.00")
        created_at = getattr(product, "created_at", None)
        data.append({
            "product_name": product.name,
            "category": product.category.name if product.category else "-",
            "price": price,
            "cost_price": cost_price,
            "profit": profit,
            "profit_percent": profit_pct,
            "is_active": bool(product.is_active),
            "created_date": timezone.localtime(created_at).date().isoformat() if created_at else "-",
        })

    return [{"rows": len(data)}], data, []


def _report_dine_in(request):
    qs = (
        _settled_order_qs(request)
        .filter(order_type="DINE_IN")
        .select_related("table", "session")
        .annotate(total_items=Coalesce(Sum("items__quantity"), 0))
        .order_by("-created_at")
    )

    data = []
    for order in qs:
        guest_count = (order.session.guest_count if order.session else None) or 1
        total_amount = order.total_amount or Decimal("0.00")
        avg_bill_value = (total_amount / Decimal(guest_count)) if guest_count else total_amount
        data.append({
            "order_no": format_order_id(order.order_number) or str(order.id)[:8],
            "date": timezone.localtime(order.created_at).date().isoformat(),
            "table_no": order.table.number if order.table else "-",
            "guest_count": guest_count,
            "total_items": order.total_items or 0,
            "total_amount": total_amount,
            "avg_bill_value": avg_bill_value,
        })

    return [{"rows": len(data)}], data, []


def _report_online(request):
    qs = (
        _settled_order_qs(request)
        .filter(order_type__in=["SWIGGY", "ZOMATO"])
        .values("order_type")
        .annotate(
            order_count=Count("id"),
            gross_sales=Coalesce(Sum("total_amount"), Decimal("0.00")),
        )
        .order_by("order_type")
    )

    data = []
    for row in qs:
        order_count = row["order_count"] or 0
        gross_sales = row["gross_sales"] or Decimal("0.00")
        commission = Decimal("0.00")
        net_settlement = gross_sales - commission
        data.append({
            "platform": "Swiggy" if row["order_type"] == "SWIGGY" else "Zomato",
            "order_count": order_count,
            "gross_sales": gross_sales,
            "commission": commission,
            "net_settlement": net_settlement,
            "avg_order_value": (gross_sales / order_count) if order_count else Decimal("0.00"),
        })

    return [{"rows": len(data)}], data, []


def _report_combo(request):
    qs = _settled_order_qs(request)
    rows = (
        OrderItem.objects.filter(order__in=qs, combo__isnull=False)
        .values("combo__name")
        .annotate(
            quantity_sold=Coalesce(Sum("quantity"), 0),
            revenue=Coalesce(Sum(F("quantity") * F("price_at_time")), Decimal("0.00")),
        )
        .order_by("-quantity_sold")
    )

    data = []
    for row in rows:
        revenue = row["revenue"] or Decimal("0.00")
        discount = Decimal("0.00")
        net_revenue = revenue - discount
        data.append({
            "combo_name": row["combo__name"] or "Unknown",
            "quantity_sold": row["quantity_sold"] or 0,
            "revenue": revenue,
            "discount": discount,
            "net_revenue": net_revenue,
            "profit": net_revenue,
        })

    return [{"rows": len(data)}], data, []


def _report_simple(request, report_key):
    if report_key == "gst":
        rows = []
        for o in _settled_order_qs(request).prefetch_related("items").order_by("-created_at"):
            taxable = sum(((it.base_price or Decimal("0")) * it.quantity for it in o.items.all()), Decimal("0.00"))
            gst = sum(((it.gst_amount or Decimal("0")) * it.quantity for it in o.items.all()), Decimal("0.00"))
            rows.append({
                "invoice_no": format_bill_number(o.bill_number) or "-",
                "date": str(timezone.localtime(o.created_at).date()),
                "customer_name": o.customer_name or "Walk-in",
                "taxable_amount": taxable,
                "cgst": gst / Decimal("2.00"),
                "sgst": gst / Decimal("2.00"),
                "igst": Decimal("0.00"),
                "total_gst": gst,
                "grand_total": o.total_amount,
                "hsn_code": "-",
            })
        return [{"rows": len(rows)}], rows, []
    if report_key == "staff-attendance":
        start, end = _resolve_date_range(request)
        logs = _apply_date(
            StaffSessionLog.objects.filter(user__role="STAFF").select_related("user"),
            "login_at__date",
            start,
            end,
        ).order_by("-login_at")
        rows = []
        for log in logs:
            login_at = timezone.localtime(log.login_at)
            logout_at = timezone.localtime(log.logout_at) if log.logout_at else None
            worked_seconds = 0
            if logout_at and logout_at > login_at:
                worked_seconds = int((logout_at - login_at).total_seconds())
            rows.append({
                "staff_name": log.user.username,
                "role": "STAFF",
                "date": login_at.date().isoformat(),
                "check_in": login_at.strftime("%H:%M:%S"),
                "check_out": logout_at.strftime("%H:%M:%S") if logout_at else None,
                "total_hours": round(worked_seconds / 3600, 2),
                "status": "Present" if logout_at else "Active",
            })
        return [{"rows": len(rows)}], rows, []
    if report_key == "staff-login":
        start, end = _resolve_date_range(request)
        logs = _apply_date(StaffSessionLog.objects.filter(user__role="STAFF").select_related("user"), "login_at__date", start, end).order_by("-login_at")
        rows = [{
            "staff_name": l.user.username,
            "punch_in": timezone.localtime(l.login_at).isoformat(),
            "punch_out": timezone.localtime(l.logout_at).isoformat() if l.logout_at else "-",
            "login_time": timezone.localtime(l.login_at).isoformat(),
            "logout_time": timezone.localtime(l.logout_at).isoformat() if l.logout_at else "-",
            "duration": round(((timezone.localtime(l.logout_at) - timezone.localtime(l.login_at)).total_seconds() / 3600), 2) if l.logout_at else 0,
            "device": "-",
            "ip_address": "-",
        } for l in logs]
        return [{"rows": len(rows)}], rows, []
    if report_key == "delivery":
        qs = _settled_order_qs(request).filter(order_type__in=["SWIGGY", "ZOMATO"]).prefetch_related("payments").order_by("-created_at")
        rows = []
        for order in qs:
            success_payments = [p for p in order.payments.all() if p.status == "SUCCESS"]
            success_payments.sort(key=lambda p: p.paid_at, reverse=True)
            latest_payment = success_payments[0] if success_payments else None
            payment_modes = sorted({p.method for p in success_payments})
            rows.append({
                "order_no": format_order_id(order.order_number) or str(order.id)[:8],
                "date": timezone.localtime(order.created_at).date().isoformat(),
                "customer_name": (order.customer.name if order.customer else None) or order.customer_name or "Walk-in",
                "platform": order.get_order_type_display(),
                "delivery_time": timezone.localtime(latest_payment.paid_at).strftime("%H:%M:%S") if latest_payment else "-",
                "amount": order.total_amount or Decimal("0.00"),
                "payment_mode": ", ".join(payment_modes) if payment_modes else "-",
            })
        return [{"rows": len(rows)}], rows, []
    return [{"note": "Placeholder data based on current schema limitations"}], [], []


REPORT_BUILDERS = {
    "daily-sales": _report_daily_sales,
    "product-wise-sales": _report_product_wise,
    "payment-method": _report_payment_method,
    "discount": _report_discount,
    "cancelled-void": _report_cancelled,
    "kot": _report_kot,
    "customer": _report_customer,
    "peak-sales-time": _report_peak_time,
    "purchase": _report_purchase,
    "supplier-wise": _report_supplier_wise,
    "stock-range": _report_stock,
    "stock-consumption": _report_stock_consumption,
    "wastage": _report_wastage,
    "low-stock": _report_low_stock,
    "ingredient": _report_ingredient,
    "menu": _report_menu,
    "dine-in": _report_dine_in,
    "online": _report_online,
    "combo": _report_combo,
}


REPORT_NAME = {
    "daily-sales": "Daily Sales Report",
    "product-wise-sales": "Product Wise Sales Report",
    "payment-method": "Payment Method Report",
    "discount": "Discount Report",
    "cancelled-void": "Cancelled / Void Report",
    "kot": "KOT Report",
    "customer": "Customer Report",
    "purchase": "Purchase Report",
    "supplier-wise": "Supplier Wise Report",
    "stock-range": "Stock Report (Date Range)",
    "stock-consumption": "Stock Consumption Report",
    "wastage": "Wastage Report",
    "low-stock": "Low Stock Report",
    "ingredient": "Ingredient Report",
    "menu": "Menu Report",
    "staff-attendance": "Staff Attendance Report",
    "staff-login": "Staff Login Report",
    "gst": "GST Report",
    "expense": "Expense Report",
    "delivery": "Delivery Report",
    "dine-in": "Dine In Report",
    "online": "Online Report",
    "combo": "Combo Report",
    "peak-sales-time": "Peak Sales Time Report",
}


class ReportByKeyView(APIView):
    permission_classes = [IsAdminOrStaff]

    def get(self, request, report_key):
        admin_only_audit_keys = {"stock-audit", "stock-difference", "audit-mismatch"}
        if (getattr(request.user, "role", "") or "").upper() == "STAFF" and report_key in admin_only_audit_keys:
            return Response({"detail": "This report is available for admin users only."}, status=403)
        report_name = REPORT_NAME.get(report_key, report_key)
        builder = REPORT_BUILDERS.get(report_key)
        if builder:
            summary, data, product_breakdown = builder(request)
            return _payload(request, report_name, summary=summary, data=data, product_breakdown=product_breakdown)
        summary, data, product_breakdown = _report_simple(request, report_key)
        return _payload(request, report_name, summary=summary, data=data, product_breakdown=product_breakdown)


class DashboardSummaryView(APIView):
    permission_classes = [IsAdminOrStaff]

    def get(self, request):
        today = timezone.localdate()
        qs = _order_qs(request).filter(created_at__date=today)
        paid_or_completed = qs.filter(Q(payment_status="PAID") | Q(status="COMPLETED")).exclude(status="CANCELLED")
        sales = paid_or_completed.aggregate(v=Coalesce(Sum("total_amount"), Decimal("0.00")))["v"] or Decimal("0.00")
        orders_count = paid_or_completed.count()

        metrics = [
            {"metric": "Total Sales", "value": sales},
            {"metric": "Total Orders", "value": orders_count},
        ]
        return _payload(request, "Dashboard Summary", summary=metrics, data=metrics)


class LegacyBlockedView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        return _payload(request, "Legacy Report", summary=[{"note": "Use new report endpoints"}], data=[])


class CouponUsageReportView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        queryset = CouponUsage.objects.select_related("coupon", "order", "user").all()

        q = str(request.query_params.get("q", "")).strip()
        if q:
            conditions = (
                Q(coupon__code__icontains=q)
                | Q(customer_phone__icontains=q)
                | Q(user__username__icontains=q)
            )
            if q.isdigit():
                conditions = conditions | Q(order__order_number=int(q))
            queryset = queryset.filter(conditions)

        from_date, to_date = _resolve_date_range(request)
        queryset = _apply_date(queryset, "used_at__date", from_date, to_date).order_by("-used_at")

        serializer = CouponUsageSerializer(queryset, many=True)
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
