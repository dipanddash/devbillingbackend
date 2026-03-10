from rest_framework import serializers
from django.db import transaction
from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal

from .models import (
    Ingredient,
    PurchaseItem,
    StockLog,
    Vendor,
    PurchaseInvoice
)


# -----------------------
# INGREDIENT
# -----------------------

class IngredientSerializer(serializers.ModelSerializer):

    class Meta:
        model = Ingredient
        fields = "__all__"


# -----------------------
# VENDOR
# -----------------------

class VendorSerializer(serializers.ModelSerializer):
    contact = serializers.CharField(source="contact_person", required=False, allow_null=True, allow_blank=True)
    lastDelivery = serializers.SerializerMethodField()
    monthlySpend = serializers.SerializerMethodField()

    class Meta:
        model = Vendor
        fields = [
            "id",
            "name",
            "category",
            "contact",
            "phone",
            "email",
            "city",
            "address",
            "created_at",
            "lastDelivery",
            "monthlySpend",
        ]

    def get_lastDelivery(self, obj):
        last_invoice = obj.purchaseinvoice_set.order_by("-created_at").first()
        if not last_invoice:
            return None
        return last_invoice.created_at.date().isoformat()

    def get_monthlySpend(self, obj):
        today = timezone.localdate()
        total = (
            PurchaseItem.objects
            .filter(
                invoice__vendor=obj,
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
        return total


class VendorHistorySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    invoice_number = serializers.CharField()
    date = serializers.DateField()
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)


# -----------------------
# PURCHASE ITEM
# -----------------------

class PurchaseItemSerializer(serializers.ModelSerializer):

    class Meta:
        model = PurchaseItem
        fields = [
            "ingredient",
            "quantity",
            "unit_price"
        ]


# -----------------------
# PURCHASE INVOICE
# -----------------------

class PurchaseInvoiceSerializer(serializers.ModelSerializer):

    items = PurchaseItemSerializer(many=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            "id",
            "vendor",
            "invoice_number",
            "items",
            "created_at"
        ]

        read_only_fields = [
            "id",
            "created_at"
        ]


    def create(self, validated_data):

        items_data = validated_data.pop("items")

        # ✅ GET USER SAFELY
        request = self.context.get("request")

        user = (
            request.user
            if request and request.user.is_authenticated
            else None
        )


        with transaction.atomic():

            # -----------------------
            # CREATE INVOICE
            # -----------------------

            invoice = PurchaseInvoice.objects.create(
                purchased_by=user,   # 🔥 SAVE STAFF NAME
                **validated_data
            )


            # -----------------------
            # CREATE ITEMS + UPDATE STOCK
            # -----------------------

            for item in items_data:

                ingredient = item["ingredient"]
                qty = item["quantity"]
                price = item["unit_price"]


                # Create purchase item
                PurchaseItem.objects.create(
                    invoice=invoice,
                    ingredient=ingredient,
                    quantity=qty,
                    unit_price=price
                )


                # Update stock
                ingredient.current_stock += qty
                ingredient.save()


                # Stock log
                StockLog.objects.create(
                    ingredient=ingredient,
                    change=qty,
                    reason="PURCHASE",
                    user=user
                )


        return invoice
