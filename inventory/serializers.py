from rest_framework import serializers
from django.db import transaction
from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal

from .models import (
    DEFAULT_INGREDIENT_CATEGORY_UUID,
    Ingredient,
    IngredientCategory,
    PurchaseItem,
    StockLog,
    Vendor,
    PurchaseInvoice
)


# -----------------------
# INGREDIENT
# -----------------------

class IngredientSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=IngredientCategory.objects.all(),
        required=False,
    )
    category_name = serializers.CharField(source="category.name", read_only=True)
    valuation = serializers.SerializerMethodField(read_only=True)
    health = serializers.SerializerMethodField(read_only=True)
    reorder_qty = serializers.SerializerMethodField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        db_alias = self.context.get("db_alias")
        if db_alias:
            self.fields["category_id"].queryset = IngredientCategory.objects.using(db_alias).all()

    class Meta:
        model = Ingredient
        fields = [
            "id",
            "name",
            "category_id",
            "category_name",
            "unit",
            "unit_price",
            "current_stock",
            "min_stock",
            "is_active",
            "valuation",
            "health",
            "reorder_qty",
        ]

    def validate_unit_price(self, value):
        if value is None:
            raise serializers.ValidationError("Unit price is required.")
        if value <= 0:
            raise serializers.ValidationError("Unit price must be greater than zero.")
        return value

    def validate_current_stock(self, value):
        if value is None:
            return Decimal("0.000")
        if value < 0:
            raise serializers.ValidationError("Current stock cannot be negative.")
        return value

    def validate_min_stock(self, value):
        if value is None:
            return Decimal("0.000")
        if value < 0:
            raise serializers.ValidationError("Minimum stock cannot be negative.")
        return value

    def create(self, validated_data):
        db_alias = self.context.get("db_alias")
        if "category" not in validated_data:
            category_qs = IngredientCategory.objects.using(db_alias) if db_alias else IngredientCategory.objects
            default_category = category_qs.filter(id=DEFAULT_INGREDIENT_CATEGORY_UUID).first()
            if default_category is not None:
                validated_data["category"] = default_category
        instance = Ingredient(**validated_data)
        if db_alias:
            instance.save(using=db_alias)
        else:
            instance.save()
        return instance

    def update(self, instance, validated_data):
        db_alias = self.context.get("db_alias")
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if db_alias:
            instance.save(using=db_alias)
        else:
            instance.save()
        return instance

    def get_valuation(self, obj):
        return (Decimal(str(obj.current_stock or 0)) * Decimal(str(obj.unit_price or 0))).quantize(Decimal("0.01"))

    def get_health(self, obj):
        current = Decimal(str(obj.current_stock or 0))
        minimum = Decimal(str(obj.min_stock or 0))
        if current <= 0:
            return "out"
        if current <= minimum:
            return "low"
        return "good"

    def get_reorder_qty(self, obj):
        current = Decimal(str(obj.current_stock or 0))
        minimum = Decimal(str(obj.min_stock or 0))
        if current <= minimum:
            return (minimum * Decimal("2") - current).quantize(Decimal("0.001"))
        return Decimal("0.000")


class IngredientCategorySerializer(serializers.ModelSerializer):
    ingredients_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = IngredientCategory
        fields = [
            "id",
            "name",
            "is_active",
            "ingredients_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_name(self, value):
        normalized = (value or "").strip().upper()
        if not normalized:
            raise serializers.ValidationError("Category name is required.")
        db_alias = self.context.get("db_alias")
        queryset = IngredientCategory.objects.using(db_alias) if db_alias else IngredientCategory.objects
        queryset = queryset.filter(name=normalized)
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        if queryset.exists():
            raise serializers.ValidationError("Category already exists.")
        return normalized

    def create(self, validated_data):
        db_alias = self.context.get("db_alias")
        instance = IngredientCategory(**validated_data)
        if db_alias:
            instance.save(using=db_alias)
        else:
            instance.save()
        return instance

    def update(self, instance, validated_data):
        db_alias = self.context.get("db_alias")
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if db_alias:
            instance.save(using=db_alias)
        else:
            instance.save()
        return instance

    def get_ingredients_count(self, obj):
        return obj.ingredients.count()


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
