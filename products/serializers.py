from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from inventory.models import DailyIngredientStock, Ingredient
from .models import Addon, Category, Combo, ComboItem, Product, Recipe


def _today_remaining_map(ingredient_ids):
    today = timezone.localdate()
    return {
        str(row.ingredient_id): max(Decimal("0.000"), Decimal(str(row.assigned_stock)) - Decimal(str(row.consumed_stock)))
        for row in DailyIngredientStock.objects.filter(date=today, ingredient_id__in=ingredient_ids)
    }


class CategorySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = "__all__"

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    image_url = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()
    availability_reason = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = "__all__"

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def _ingredient_shortage_for_product(self, product, multiplier=Decimal("1")):
        recipes = list(product.recipes.all())
        if not recipes:
            return "Recipe not configured"

        ingredient_ids = [recipe.ingredient_id for recipe in recipes if recipe.ingredient_id]
        remaining_map = _today_remaining_map(ingredient_ids)

        for recipe in recipes:
            ingredient = recipe.ingredient
            if ingredient is None:
                return "Recipe ingredient missing"
            required = Decimal(str(recipe.quantity)) * Decimal(str(multiplier))
            if Decimal(str(ingredient.current_stock)) < required:
                return (
                    f"Insufficient stock: {ingredient.name} "
                    f"(need {required}, have {ingredient.current_stock})"
                )
            remaining_today = remaining_map.get(str(recipe.ingredient_id))
            if remaining_today is None:
                return "Opening stock is not available. Please contact admin."
            if remaining_today < required:
                return f"Assigned stock for today is exhausted: {ingredient.name}"
        return None

    def get_is_available(self, obj):
        return self._ingredient_shortage_for_product(obj) is None

    def get_availability_reason(self, obj):
        return self._ingredient_shortage_for_product(obj)


class AddonSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    ingredient_id = serializers.PrimaryKeyRelatedField(
        source="ingredient",
        queryset=Ingredient.objects.all(),
        required=False,
        allow_null=True,
    )
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    ingredient_unit = serializers.CharField(source="ingredient.unit", read_only=True)
    ingredient_category_id = serializers.CharField(source="ingredient.category_id", read_only=True)
    ingredient_category_name = serializers.CharField(source="ingredient.category.name", read_only=True)

    class Meta:
        model = Addon
        fields = [
            "id",
            "name",
            "price",
            "image",
            "image_url",
            "ingredient_id",
            "ingredient_name",
            "ingredient_unit",
            "ingredient_category_id",
            "ingredient_category_name",
            "ingredient_quantity",
        ]

    def validate(self, attrs):
        ingredient = attrs.get("ingredient", getattr(self.instance, "ingredient", None))
        ingredient_quantity = attrs.get(
            "ingredient_quantity",
            getattr(self.instance, "ingredient_quantity", Decimal("0")),
        )

        ingredient_quantity = Decimal(str(ingredient_quantity or 0))
        if ingredient and ingredient_quantity <= 0:
            raise serializers.ValidationError(
                {"ingredient_quantity": "Ingredient quantity must be greater than zero."}
            )
        if not ingredient:
            attrs["ingredient_quantity"] = Decimal("0")
        return attrs

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None


class ComboSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()
    availability_reason = serializers.SerializerMethodField()

    class Meta:
        model = Combo
        fields = "__all__"

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def get_items(self, obj):
        combo_items = obj.items.all()
        return ComboItemSerializer(combo_items, many=True, context=self.context).data

    def _combo_shortage_reason(self, combo):
        combo_items = list(combo.items.all())
        if not combo_items:
            return "Combo has no products"

        needed_by_ingredient = {}
        for combo_item in combo_items:
            product = combo_item.product
            recipes = list(product.recipes.all())
            if not recipes:
                return f"Recipe not configured: {product.name}"

            for recipe in recipes:
                ingredient = recipe.ingredient
                if ingredient is None:
                    return f"Recipe ingredient missing: {product.name}"

                key = str(ingredient.id)
                needed = Decimal(str(recipe.quantity)) * Decimal(str(combo_item.quantity))
                if key not in needed_by_ingredient:
                    needed_by_ingredient[key] = {
                        "ingredient_name": ingredient.name,
                        "stock": Decimal(str(ingredient.current_stock)),
                        "needed": Decimal("0"),
                    }
                needed_by_ingredient[key]["needed"] += needed

        for data in needed_by_ingredient.values():
            if data["stock"] < data["needed"]:
                return (
                    f"Insufficient stock: {data['ingredient_name']} "
                    f"(need {data['needed']}, have {data['stock']})"
                )
        remaining_map = _today_remaining_map(needed_by_ingredient.keys())
        for ingredient_id, data in needed_by_ingredient.items():
            remaining_today = remaining_map.get(str(ingredient_id))
            if remaining_today is None:
                return "Opening stock is not available. Please contact admin."
            if remaining_today < data["needed"]:
                return f"Assigned stock for today is exhausted: {data['ingredient_name']}"
        return None

    def get_is_available(self, obj):
        return self._combo_shortage_reason(obj) is None

    def get_availability_reason(self, obj):
        return self._combo_shortage_reason(obj)


class ComboItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    combo_name = serializers.CharField(source="combo.name", read_only=True)

    class Meta:
        model = ComboItem
        fields = [
            "id",
            "combo",
            "combo_name",
            "product",
            "product_name",
            "quantity",
        ]
        read_only_fields = [
            "id",
            "combo_name",
            "product_name",
        ]
        extra_kwargs = {
            "combo": {"required": False},
        }


class ComboNestedItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComboItem
        fields = [
            "product",
            "quantity",
        ]


class ComboWithItemsSerializer(serializers.ModelSerializer):
    items = ComboNestedItemSerializer(many=True, required=False)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Combo
        fields = [
            "id",
            "name",
            "price",
            "gst_percent",
            "image",
            "image_url",
            "is_active",
            "created_at",
            "items",
        ]
        read_only_fields = ["id", "created_at"]

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        with transaction.atomic():
            combo = Combo.objects.create(**validated_data)
            for item in items_data:
                ComboItem.objects.create(combo=combo, **item)
        return combo

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)

        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()

        if items_data is not None:
            with transaction.atomic():
                instance.items.all().delete()
                for item in items_data:
                    ComboItem.objects.create(combo=instance, **item)

        return instance


class RecipeSerializer(serializers.ModelSerializer):
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = Recipe
        fields = "__all__"
