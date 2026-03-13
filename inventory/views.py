from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.db import transaction
from decimal import Decimal
from uuid import uuid4
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.permissions import IsAdminOrStaff, IsAdminRole
from accounts.models import User

from .models import (
    Ingredient,
    Vendor,
    PurchaseInvoice,
    PurchaseItem,
    OpeningStock,
    ManualClosing,
    DailyStockSnapshot,
    StockLog,
)
from .serializers import (
    IngredientSerializer,
    VendorSerializer,
    PurchaseInvoiceSerializer,
)
from rest_framework.parsers import JSONParser
from cafe_billing_backend.connectivity import is_neon_reachable

# -----------------------
# INGREDIENT APIs
# -----------------------


def _inventory_db_alias(request=None):
    if request is not None and getattr(request, "is_offline", False):
        return "sqlite"
    return "sqlite" if not is_neon_reachable(force=False) else "neon"

class IngredientListCreateView(generics.ListCreateAPIView):

    queryset = Ingredient.objects.all().order_by("name")
    serializer_class = IngredientSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrStaff()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        ingredient = serializer.save()
        if getattr(self.request, "is_offline", False) or not is_neon_reachable(force=False):
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="ingredient",
                action="create",
                payload={
                    "id": str(ingredient.id),
                    "name": ingredient.name,
                    "unit": ingredient.unit,
                    "current_stock": str(ingredient.current_stock),
                    "min_stock": str(ingredient.min_stock),
                },
            )


class IngredientDetailView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    



class IngredientUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdminRole]
    parser_classes = [JSONParser]

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

        if OpeningStock.objects.using(db_alias).exists():
            return Response({"error": "Opening stock has already been initialized."}, status=400)

        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            return Response({"error": "items must be a non-empty list."}, status=400)

        ingredient_ids = [row.get("ingredient") for row in items if isinstance(row, dict)]
        ingredients = {
            str(i.id): i for i in Ingredient.objects.using(db_alias).filter(id__in=ingredient_ids)
        }
        user = request.user if request.user.is_authenticated else None

        openings = []
        logs = []
        touched = []
        seen_ids = set()

        for row in items:
            if not isinstance(row, dict):
                return Response({"error": "Each item must be an object."}, status=400)
            ing_id = str(row.get("ingredient") or "").strip()
            qty_raw = row.get("quantity")
            ingredient = ingredients.get(ing_id)
            if not ingredient:
                return Response({"error": f"Invalid ingredient id: {ing_id}"}, status=400)
            if ing_id in seen_ids:
                return Response({"error": f"Duplicate ingredient in payload: {ing_id}"}, status=400)
            seen_ids.add(ing_id)
            try:
                qty = Decimal(str(qty_raw))
            except Exception:  # noqa: BLE001
                return Response({"error": f"Invalid quantity for ingredient: {ingredient.name}"}, status=400)
            if qty < 0:
                return Response({"error": f"Quantity cannot be negative for ingredient: {ingredient.name}"}, status=400)

            ingredient.current_stock = qty
            touched.append(ingredient)
            openings.append(OpeningStock(ingredient=ingredient, quantity=qty, set_by=user))
            logs.append(StockLog(ingredient=ingredient, change=qty, reason="OPENING", user=user))

        with transaction.atomic(using=db_alias):
            OpeningStock.objects.using(db_alias).bulk_create(openings)
            Ingredient.objects.using(db_alias).bulk_update(touched, ["current_stock"])
            StockLog.objects.using(db_alias).bulk_create(logs)

        if db_alias == "sqlite":
            from sync.models import OfflineSyncQueue

            OfflineSyncQueue.objects.using("sqlite").create(
                client_id=uuid4(),
                entity_type="opening_stock",
                action="init",
                payload={
                    "items": [
                        {
                            "ingredient_id": str(opening.ingredient_id),
                            "quantity": str(opening.quantity),
                        }
                        for opening in openings
                    ],
                    "set_by_id": str(user.id) if user else None,
                },
            )

        return Response({"message": "Opening stock initialized.", "count": len(openings)}, status=201)


class OpeningStockStatusView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        qs = OpeningStock.objects.all().order_by("created_at")
        first = qs.first()
        return Response(
            {
                "initialized": qs.exists(),
                "count": qs.count(),
                "initialized_on": timezone.localtime(first.created_at).isoformat() if first else None,
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
