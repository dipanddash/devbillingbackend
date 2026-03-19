from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce
from django.db import transaction
from decimal import Decimal
from uuid import uuid4
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.permissions import IsAdminOrStaff, IsAdminRole
from accounts.models import User

from .models import (
    DEFAULT_INGREDIENT_CATEGORY_UUID,
    Ingredient,
    IngredientCategory,
    Vendor,
    PurchaseInvoice,
    PurchaseItem,
    OpeningStock,
    DailyIngredientStock,
    ManualClosing,
    DailyStockSnapshot,
    StockLog,
)
from .serializers import (
    IngredientSerializer,
    IngredientCategorySerializer,
    VendorSerializer,
    PurchaseInvoiceSerializer,
)
from .stock_service import build_daily_summary, upsert_daily_assignment
from rest_framework.parsers import JSONParser
from cafe_billing_backend.connectivity import is_neon_reachable

# -----------------------
# INGREDIENT APIs
# -----------------------


def _inventory_db_alias(request=None):
    if request is not None and getattr(request, "is_offline", False):
        return "sqlite"
    return "sqlite" if not is_neon_reachable(force=False) else "neon"


def _extract_validation_message(exc):
    detail = getattr(exc, "detail", exc)
    if isinstance(detail, (list, tuple)) and detail:
        first = detail[0]
        if isinstance(first, (list, tuple)) and first:
            return str(first[0])
        return str(first)
    if isinstance(detail, dict) and detail:
        first_val = next(iter(detail.values()))
        if isinstance(first_val, (list, tuple)) and first_val:
            return str(first_val[0])
        return str(first_val)
    return str(detail)


def _ensure_default_ingredient_category(db_alias="neon"):
    manager = IngredientCategory.objects.using(db_alias) if db_alias else IngredientCategory.objects
    category, _ = manager.get_or_create(
        id=DEFAULT_INGREDIENT_CATEGORY_UUID,
        defaults={"name": "OTHERS", "is_active": True},
    )
    return category


class IngredientCategoryListCreateView(generics.ListCreateAPIView):
    serializer_class = IngredientCategorySerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["db_alias"] = _inventory_db_alias(self.request)
        return context

    def get_queryset(self):
        db_alias = _inventory_db_alias(self.request)
        _ensure_default_ingredient_category(db_alias=db_alias)
        queryset = IngredientCategory.objects.using(db_alias).all().order_by("name")
        search = (self.request.query_params.get("search") or "").strip()
        is_active = self.request.query_params.get("is_active")
        if search:
            queryset = queryset.filter(name__icontains=search)
        if is_active in {"true", "false"}:
            queryset = queryset.filter(is_active=(is_active == "true"))
        return queryset

    def perform_create(self, serializer):
        db_alias = _inventory_db_alias(self.request)
        category = serializer.save()
        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="ingredient_category",
                action="create",
                payload={
                    "id": str(category.id),
                    "name": category.name,
                    "is_active": bool(category.is_active),
                },
            )


class IngredientCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IngredientCategorySerializer
    parser_classes = [JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["db_alias"] = _inventory_db_alias(self.request)
        return context

    def get_queryset(self):
        db_alias = _inventory_db_alias(self.request)
        return IngredientCategory.objects.using(db_alias).all()

    def perform_destroy(self, instance):
        db_alias = instance._state.db or _inventory_db_alias(self.request)
        default_category = _ensure_default_ingredient_category(db_alias=db_alias)
        if instance.id == default_category.id:
            raise ValidationError("Default category OTHERS cannot be deleted.")
        Ingredient.objects.using(db_alias).filter(category=instance).update(category=default_category)
        instance.delete(using=db_alias)

        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="ingredient_category",
                action="delete",
                payload={"id": str(instance.id)},
            )

    def perform_update(self, serializer):
        db_alias = _inventory_db_alias(self.request)
        category = serializer.save()
        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="ingredient_category",
                action="update",
                payload={
                    "id": str(category.id),
                    "name": category.name,
                    "is_active": bool(category.is_active),
                },
            )

class IngredientListCreateView(generics.ListCreateAPIView):
    serializer_class = IngredientSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrStaff()]
        return [IsAuthenticated()]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["db_alias"] = _inventory_db_alias(self.request)
        return context

    def get_queryset(self):
        db_alias = _inventory_db_alias(self.request)
        queryset = Ingredient.objects.using(db_alias).select_related("category").all()
        category_id = (
            self.request.query_params.get("category_id")
            or self.request.query_params.get("category")
            or ""
        ).strip()
        search = (self.request.query_params.get("search") or "").strip()
        health = (self.request.query_params.get("health") or "").strip().lower()
        sort = (self.request.query_params.get("sort") or "").strip().lower()

        if category_id and category_id.lower() != "all":
            queryset = queryset.filter(category_id=category_id)
        if search:
            queryset = queryset.filter(name__icontains=search)
        if health == "out":
            queryset = queryset.filter(current_stock__lte=0)
        elif health == "low":
            queryset = queryset.filter(current_stock__gt=0, current_stock__lte=F("min_stock"))
        elif health in {"healthy", "good"}:
            queryset = queryset.filter(current_stock__gt=F("min_stock"))

        if sort == "stock":
            queryset = queryset.order_by("-current_stock", "name")
        elif sort == "valuation":
            queryset = queryset.annotate(
                valuation_sort=ExpressionWrapper(
                    F("current_stock") * F("unit_price"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            ).order_by("-valuation_sort", "name")
        else:
            queryset = queryset.order_by("name")
        return queryset

    def perform_create(self, serializer):
        db_alias = _inventory_db_alias(self.request)
        _ensure_default_ingredient_category(db_alias=db_alias)
        ingredient = serializer.save()
        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="ingredient",
                action="create",
                payload={
                    "id": str(ingredient.id),
                    "name": ingredient.name,
                    "category_id": str(ingredient.category_id) if ingredient.category_id else None,
                    "category_name": ingredient.category.name if ingredient.category else "OTHERS",
                    "unit": ingredient.unit,
                    "unit_price": str(ingredient.unit_price),
                    "current_stock": str(ingredient.current_stock),
                    "min_stock": str(ingredient.min_stock),
                    "is_active": ingredient.is_active,
                },
            )


class IngredientUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IngredientSerializer
    parser_classes = [JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["db_alias"] = _inventory_db_alias(self.request)
        return context

    def get_queryset(self):
        db_alias = _inventory_db_alias(self.request)
        return Ingredient.objects.using(db_alias).select_related("category").all()

# -----------------------
# VENDOR APIs
# -----------------------

class VendorListCreateView(generics.ListCreateAPIView):

    queryset = Vendor.objects.all().order_by("name")
    serializer_class = VendorSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        vendor = serializer.save()
        if getattr(self.request, "is_offline", False) or not is_neon_reachable(force=False):
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="vendor",
                action="create",
                payload={
                    "id": str(vendor.id),
                    "name": vendor.name,
                    "category": vendor.category or "",
                    "contact_person": vendor.contact_person or "",
                    "phone": vendor.phone or "",
                    "email": vendor.email or "",
                    "city": vendor.city or "",
                    "address": vendor.address or "",
                },
            )


class VendorDetailView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer
    permission_classes = [IsAuthenticated]


class VendorHistoryView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            vendor = Vendor.objects.get(pk=pk)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=404)

        invoices = (
            PurchaseInvoice.objects
            .filter(vendor=vendor)
            .annotate(
                total_amount=Coalesce(
                    Sum(
                        F("items__quantity") * F("items__unit_price"),
                        output_field=DecimalField(max_digits=12, decimal_places=2)
                    ),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
            .order_by("-created_at")
        )

        invoice_rows = []
        for inv in invoices:
            invoice_rows.append({
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "date": inv.created_at.date().isoformat(),
                "total_amount": inv.total_amount,
            })

        lifetime_spend = (
            PurchaseItem.objects
            .filter(invoice__vendor=vendor)
            .aggregate(
                total=Coalesce(
                    Sum(
                        F("quantity") * F("unit_price"),
                        output_field=DecimalField(max_digits=12, decimal_places=2)
                    ),
                    Decimal("0.00"),
                    output_field=DecimalField(max_digits=12, decimal_places=2)
                )
            )["total"]
        )

        today = timezone.localdate()
        monthly_spend = (
            PurchaseItem.objects
            .filter(
                invoice__vendor=vendor,
                invoice__created_at__year=today.year,
                invoice__created_at__month=today.month
            )
            .aggregate(
                total=Coalesce(
                    Sum(
                        F("quantity") * F("unit_price"),
                        output_field=DecimalField(max_digits=12, decimal_places=2)
                    ),
                    Decimal("0.00"),
                    output_field=DecimalField(max_digits=12, decimal_places=2)
                )
            )["total"]
        )

        last_invoice = invoices.first()
        vendor_data = VendorSerializer(vendor).data

        return Response(
            {
                "vendor": vendor_data,
                "summary": {
                    "total_invoices": invoices.count(),
                    "lifetime_spend": lifetime_spend,
                    "monthly_spend": monthly_spend,
                    "last_delivery": last_invoice.created_at.date().isoformat() if last_invoice else None,
                },
                "history": invoice_rows,
            },
            status=200
        )


# -----------------------
# PURCHASE INVOICE
# -----------------------

class PurchaseInvoiceCreateView(generics.CreateAPIView):

    queryset = PurchaseInvoice.objects.all()
    serializer_class = PurchaseInvoiceSerializer
    permission_classes = [IsAdminOrStaff]


    # ✅ VERY IMPORTANT
    def get_serializer_context(self):

        context = super().get_serializer_context()
        context["request"] = self.request
        return context


def _sum_stock_change(ingredient, date, reason, positive=True):
    logs = StockLog.objects.filter(
        ingredient=ingredient,
        reason=reason,
        created_at__date=date,
    )
    if positive:
        return logs.filter(change__gt=0).aggregate(v=Coalesce(Sum("change"), Decimal("0.000")))["v"] or Decimal("0.000")
    return logs.filter(change__lt=0).aggregate(v=Coalesce(Sum(-F("change")), Decimal("0.000")))["v"] or Decimal("0.000")


def _base_stock_before_date(ingredient, date):
    previous_daily = (
        DailyIngredientStock.objects
        .filter(ingredient=ingredient, date__lt=date)
        .order_by("-date")
        .first()
    )
    if previous_daily:
        remaining = previous_daily.assigned_stock - previous_daily.consumed_stock
        if remaining > 0:
            return remaining

    previous_manual = (
        ManualClosing.objects
        .filter(ingredient=ingredient, date__lt=date)
        .order_by("-date", "-created_at")
        .first()
    )
    if previous_manual:
        return previous_manual.physical_quantity

    opening = OpeningStock.objects.filter(ingredient=ingredient).first()
    return opening.quantity if opening else Decimal("0.000")


def _system_closing_for_date(ingredient, date):
    base = _base_stock_before_date(ingredient, date)
    purchased = _sum_stock_change(ingredient, date, "PURCHASE", positive=True)
    sold = _sum_stock_change(ingredient, date, "SALE", positive=False)
    start_stock = base + purchased
    system_closing = start_stock - sold
    return {
        "base_stock": base,
        "purchased": purchased,
        "sold": sold,
        "start_stock": start_stock,
        "system_closing": system_closing,
    }


class OpeningStockInitView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request):
        db_alias = _inventory_db_alias(request)
        items = request.data.get("items")
        date_raw = request.data.get("date")
        target_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not target_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        user = request.user if request.user.is_authenticated else None
        try:
            assigned_rows = upsert_daily_assignment(
                items=items,
                target_date=target_date,
                user=user,
                db_alias=db_alias,
            )
        except ValidationError as exc:
            return Response({"error": _extract_validation_message(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return Response({"error": str(exc)}, status=400)

        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="opening_stock",
                action="init",
                payload={
                    "date": target_date.isoformat(),
                    "items": [
                        {
                            "ingredient_id": str(row.ingredient_id),
                            "quantity": str(row.assigned_stock),
                        }
                        for row in assigned_rows
                    ],
                    "set_by_id": str(user.id) if user else None,
                },
            )

        return Response(
            {
                "message": "Daily stock assigned successfully.",
                "date": target_date.isoformat(),
                "count": len(assigned_rows),
            },
            status=201,
        )


class OpeningStockStatusView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        db_alias = _inventory_db_alias(request)
        date_raw = request.GET.get("date")
        target_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not target_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        summary = build_daily_summary(target_date=target_date, db_alias=db_alias)
        totals = summary["totals"]
        assigned_today = Decimal(str(totals["assigned_today"]))
        return Response(
            {
                "date": summary["date"],
                "initialized": assigned_today > 0,
                "count": int(totals["ingredients_count"]),
                "assigned_today": str(totals["assigned_today"]),
                "used_today": str(totals["used_today"]),
                "remaining_today": str(totals["remaining_today"]),
                "valuation": str(totals["valuation"]),
            },
            status=200,
        )


class DailyStockSummaryView(APIView):
    permission_classes = [IsAdminOrStaff]

    def get(self, request):
        db_alias = _inventory_db_alias(request)
        date_raw = request.GET.get("date")
        category_id = (request.GET.get("category_id") or request.GET.get("category") or "").strip()
        target_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not target_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        summary = build_daily_summary(target_date=target_date, db_alias=db_alias)
        source_rows = summary["rows"]
        if category_id and category_id.lower() != "all":
            source_rows = [row for row in source_rows if str(row.get("category_id") or "") == category_id]

        total_assigned = Decimal("0.000")
        total_used = Decimal("0.000")
        total_remaining = Decimal("0.000")
        total_valuation = Decimal("0.00")
        rows = []
        for row in source_rows:
            total_assigned += Decimal(str(row["assigned_today"]))
            total_used += Decimal(str(row["used_today"]))
            total_remaining += Decimal(str(row["remaining_today"]))
            total_valuation += Decimal(str(row["valuation"]))
            rows.append(
                {
                    "ingredient_id": row["ingredient_id"],
                    "ingredient_name": row["ingredient_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "unit": row["unit"],
                    "unit_price": str(row["unit_price"]),
                    "assigned_today": str(row["assigned_today"]),
                    "used_today": str(row["used_today"]),
                    "remaining_today": str(row["remaining_today"]),
                    "carry_forward": str(row["carry_forward"]),
                    "total_stock": str(row["total_stock"]),
                    "min_stock": str(row["min_stock"]),
                    "valuation": str(row["valuation"]),
                    "health": row["health"],
                    "is_active": row["is_active"],
                }
            )
        return Response(
            {
                "date": summary["date"],
                "totals": {
                    "ingredients_count": len(rows),
                    "assigned_today": str(total_assigned.quantize(Decimal("0.000"))),
                    "used_today": str(total_used.quantize(Decimal("0.000"))),
                    "remaining_today": str(total_remaining.quantize(Decimal("0.000"))),
                    "valuation": str(total_valuation.quantize(Decimal("0.00"))),
                },
                "rows": rows,
            },
            status=200,
        )


class DailyStockAssignView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request):
        db_alias = _inventory_db_alias(request)
        date_raw = request.data.get("date")
        target_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not target_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        items = request.data.get("items")
        user = request.user if request.user.is_authenticated else None
        try:
            assigned_rows = upsert_daily_assignment(
                items=items,
                target_date=target_date,
                user=user,
                db_alias=db_alias,
            )
        except ValidationError as exc:
            return Response({"error": _extract_validation_message(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return Response({"error": str(exc)}, status=400)

        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="opening_stock",
                action="init",
                payload={
                    "date": target_date.isoformat(),
                    "items": [
                        {
                            "ingredient_id": str(row.ingredient_id),
                            "quantity": str(row.assigned_stock),
                        }
                        for row in assigned_rows
                    ],
                    "set_by_id": str(user.id) if user else None,
                },
            )

        summary = build_daily_summary(target_date=target_date, db_alias=db_alias)
        return Response(
            {
                "message": "Daily stock assignment saved.",
                "date": summary["date"],
                "count": len(assigned_rows),
                "totals": {
                    "assigned_today": str(summary["totals"]["assigned_today"]),
                    "used_today": str(summary["totals"]["used_today"]),
                    "remaining_today": str(summary["totals"]["remaining_today"]),
                },
            },
            status=200,
        )


class ManualClosingCreateView(APIView):
    permission_classes = [IsAdminOrStaff]

    def post(self, request):
        db_alias = _inventory_db_alias(request)
        date_raw = request.data.get("date")
        close_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not close_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            return Response({"error": "items must be a non-empty list."}, status=400)

        ingredient_ids = [row.get("ingredient") for row in items if isinstance(row, dict)]
        ingredients = {
            str(i.id): i for i in Ingredient.objects.using(db_alias).filter(id__in=ingredient_ids)
        }
        user = request.user if request.user.is_authenticated else None
        saved_rows = []

        if (getattr(request.user, "role", "") or "").upper() == "STAFF" and user:
            already_submitted = ManualClosing.objects.using(db_alias).filter(
                entered_by=user,
                date=close_date,
                physical_quantity__gt=0,
            ).exists()
            if already_submitted:
                return Response(
                    {
                        "error": (
                            "Manual closing already submitted for today. "
                            "Ask admin to reactivate your account to correct entries."
                        )
                    },
                    status=400,
                )

        with transaction.atomic(using=db_alias):
            for row in items:
                if not isinstance(row, dict):
                    return Response({"error": "Each item must be an object."}, status=400)
                ing_id = str(row.get("ingredient") or "").strip()
                qty_raw = row.get("quantity")
                ingredient = ingredients.get(ing_id)
                if not ingredient:
                    return Response({"error": f"Invalid ingredient id: {ing_id}"}, status=400)
                try:
                    qty = Decimal(str(qty_raw))
                except Exception:  # noqa: BLE001
                    return Response({"error": f"Invalid quantity for ingredient: {ingredient.name}"}, status=400)
                if qty < 0:
                    return Response({"error": f"Quantity cannot be negative for ingredient: {ingredient.name}"}, status=400)

                calc = _system_closing_for_date(ingredient, close_date)
                if qty > calc["start_stock"]:
                    return Response(
                        {
                            "error": (
                                f"Manual closing for {ingredient.name} cannot exceed start stock "
                                f"({calc['start_stock']})."
                            )
                        },
                        status=400,
                    )

                manual, _ = ManualClosing.objects.using(db_alias).update_or_create(
                    ingredient=ingredient,
                    date=close_date,
                    defaults={"physical_quantity": qty, "entered_by": user},
                )

                difference = calc["system_closing"] - qty
                DailyStockSnapshot.objects.using(db_alias).update_or_create(
                    ingredient=ingredient,
                    date=close_date,
                    defaults={
                        "system_closing": calc["system_closing"],
                        "manual_closing": qty,
                        "difference": difference,
                    },
                )

                saved_rows.append({
                    "ingredient_id": str(ingredient.id),
                    "ingredient_name": ingredient.name,
                    "manual_closing": manual.physical_quantity,
                    "date": close_date.isoformat(),
                })

            if (getattr(request.user, "role", "") or "").upper() == "STAFF":
                User.objects.using(db_alias).filter(role="STAFF", is_active=True).update(
                    is_active=False,
                    day_locked_on=close_date,
                )

        return Response({"message": "Manual closing saved.", "rows": saved_rows}, status=201)


class StaffManualClosingView(APIView):
    permission_classes = [IsAdminOrStaff]

    def get(self, request):
        date_raw = request.GET.get("date")
        close_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not close_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        qs = ManualClosing.objects.filter(date=close_date).select_related("ingredient", "entered_by").order_by("ingredient__name")
        if (getattr(request.user, "role", "") or "").upper() == "STAFF":
            qs = qs.filter(entered_by=request.user)

        rows = [{
            "ingredient_id": str(row.ingredient_id),
            "ingredient_name": row.ingredient.name if row.ingredient else "-",
            "unit": row.ingredient.unit if row.ingredient else "-",
            "physical_quantity": row.physical_quantity,
            "date": row.date.isoformat(),
            "entered_at": timezone.localtime(row.created_at).isoformat(),
            "entered_by": row.entered_by.username if row.entered_by else "-",
        } for row in qs]
        limits = []
        for ingredient in Ingredient.objects.all().order_by("name"):
            calc = _system_closing_for_date(ingredient, close_date)
            limits.append({
                "ingredient_id": str(ingredient.id),
                "ingredient_name": ingredient.name,
                "unit": ingredient.unit,
                "start_stock": calc["start_stock"],
                "system_closing": calc["system_closing"],
            })
        return Response({"date": close_date.isoformat(), "rows": rows, "limits": limits}, status=200)


class StockAuditView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        db_alias = _inventory_db_alias(request)
        date_raw = request.GET.get("date")
        audit_date = parse_date(date_raw) if date_raw else timezone.localdate()
        if not audit_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        ingredients = Ingredient.objects.using(db_alias).all().order_by("name")
        rows = []
        mismatch_count = 0

        with transaction.atomic(using=db_alias):
            for ingredient in ingredients:
                calc = _system_closing_for_date(ingredient, audit_date)
                manual = (
                    ManualClosing.objects.using(db_alias)
                    .filter(ingredient=ingredient, date=audit_date)
                    .order_by("-created_at")
                    .first()
                )
                manual_qty = manual.physical_quantity if manual else None
                difference = (calc["system_closing"] - manual_qty) if manual_qty is not None else None
                if difference is not None and difference != 0:
                    mismatch_count += 1

                DailyStockSnapshot.objects.using(db_alias).update_or_create(
                    ingredient=ingredient,
                    date=audit_date,
                    defaults={
                        "system_closing": calc["system_closing"],
                        "manual_closing": manual_qty,
                        "difference": difference,
                    },
                )

                rows.append({
                    "ingredient_id": str(ingredient.id),
                    "ingredient_name": ingredient.name,
                    "unit": ingredient.unit,
                    "previous_manual_closing": calc["base_stock"],
                    "purchase_qty": calc["purchased"],
                    "sold_qty": calc["sold"],
                    "start_stock": calc["start_stock"],
                    "system_closing": calc["system_closing"],
                    "manual_closing": manual_qty,
                    "difference": difference,
                    "entered_by": manual.entered_by.username if manual and manual.entered_by else "-",
                    "entered_at": timezone.localtime(manual.created_at).isoformat() if manual else None,
                    "has_mismatch": bool(difference is not None and difference != 0),
                })

        return Response(
            {
                "date": audit_date.isoformat(),
                "summary": {
                    "total_ingredients": len(rows),
                    "mismatch_count": mismatch_count,
                },
                "rows": rows,
            },
            status=200,
        )
