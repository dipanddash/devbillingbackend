import uuid as uuid_mod

from django.db import DatabaseError, OperationalError
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrStaff, IsAdminRole

from .models import Category, Product, Recipe, Addon, Combo, ComboItem
from .billing_catalog import build_billing_catalog_payload
from .serializers import (
    CategorySerializer,
    ProductSerializer,
    RecipeSerializer,
    AddonSerializer,
    ComboSerializer,
    ComboItemSerializer,
    ComboWithItemsSerializer,
)
from rest_framework.parsers import JSONParser
from cafe_billing_backend.connectivity import is_neon_reachable
from sync.models import OfflineSyncQueue


class SQLiteFallbackQuerysetMixin:
    """
    Read from Neon when available, but automatically fall back to SQLite if
    Neon drops or returns a database-level error during the request.
    """

    sqlite_fallback_enabled = True
    _forced_read_alias = None

    def should_prefer_sqlite(self):
        pending_catalog_statuses = ("PENDING", "IN_PROGRESS", "FAILED")
        return OfflineSyncQueue.objects.using("sqlite").filter(
            entity_type__in=("category", "product", "addon", "combo", "recipe"),
            status__in=pending_catalog_statuses,
        ).exists()

    def get_queryset_for_alias(self, alias):
        queryset = super().get_queryset()
        return queryset.using(alias)

    def get_queryset(self):
        alias = self._forced_read_alias or (
            "sqlite"
            if self.should_prefer_sqlite()
            else ("neon" if is_neon_reachable(force=True) else "sqlite")
        )
        return self.get_queryset_for_alias(alias)

    def list(self, request, *args, **kwargs):
        try:
            return super().list(request, *args, **kwargs)
        except (OperationalError, DatabaseError):
            if not self.sqlite_fallback_enabled:
                raise
            queryset = self.filter_queryset(self.get_queryset_for_alias("sqlite"))
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        try:
            return super().retrieve(request, *args, **kwargs)
        except (OperationalError, DatabaseError):
            if not self.sqlite_fallback_enabled:
                raise
            self._forced_read_alias = "sqlite"
            try:
                return super().retrieve(request, *args, **kwargs)
            finally:
                self._forced_read_alias = None


def _enqueue_catalog_create(entity_type, payload, entity_id):
    client_id = uuid_mod.uuid5(
        uuid_mod.NAMESPACE_URL,
        f"offline-catalog:{entity_type}:create:{entity_id}",
    )
    if OfflineSyncQueue.objects.using("sqlite").filter(client_id=client_id).exists():
        return
    OfflineSyncQueue.objects.using("sqlite").create(
        client_id=client_id,
        entity_type=entity_type,
        action="create",
        payload=payload,
    )


def _enqueue_recipe_sync(action, recipe_id, payload=None):
    client_id = uuid_mod.uuid5(
        uuid_mod.NAMESPACE_URL,
        f"offline-recipe:{action}:{recipe_id}",
    )
    existing = OfflineSyncQueue.objects.using("sqlite").filter(client_id=client_id).first()
    if existing:
        existing.payload = payload or {}
        existing.status = "PENDING"
        existing.retry_count = 0
        existing.error_message = ""
        existing.save(
            using="sqlite",
            update_fields=["payload", "status", "retry_count", "error_message", "updated_at"],
        )
        return
    OfflineSyncQueue.objects.using("sqlite").create(
        client_id=client_id,
        entity_type="recipe",
        action=action,
        payload=payload or {},
    )

# ----------------------------
# CATEGORY
# ----------------------------

class CategoryListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        category = serializer.save()
        if is_neon_reachable(force=True):
            return
        _enqueue_catalog_create(
            "category",
            {
                "id": str(category.id),
                "name": category.name,
            },
            category.id,
        )


class CategoryRetrieveUpdateDeleteView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def destroy(self, request, *args, **kwargs):
        category = self.get_object()
        product_count = category.products.count()
        if product_count > 0:
            return Response(
                {"detail": f"Cannot delete category \"{category.name}\" — it has {product_count} product(s). Delete them first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

# ----------------------------
# PRODUCT
# ----------------------------

class ProductListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    queryset = Product.objects.filter(is_active=True).select_related("category").prefetch_related("recipes__ingredient")
    serializer_class = ProductSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAdminOrStaff()]

    def perform_create(self, serializer):
        product = serializer.save(is_active=True)
        if is_neon_reachable(force=True):
            return
        _enqueue_catalog_create(
            "product",
            {
                "id": str(product.id),
                "name": product.name,
                "category_id": str(product.category_id),
                "price": str(product.price),
                "gst_percent": str(product.gst_percent),
                "is_active": bool(product.is_active),
            },
            product.id,
        )


class ProductUpdateView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsAdminRole]

# ----------------------------
# ADDON
# ----------------------------

class AddonListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    queryset = Addon.objects.select_related("ingredient", "ingredient__category").all()
    serializer_class = AddonSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        addon = serializer.save()
        if is_neon_reachable(force=True):
            return
        _enqueue_catalog_create(
            "addon",
            {
                "id": str(addon.id),
                "name": addon.name,
                "price": str(addon.price),
                "ingredient_id": str(addon.ingredient_id) if addon.ingredient_id else "",
                "ingredient_quantity": str(addon.ingredient_quantity or 0),
            },
            addon.id,
        )

# ----------------------------
# COMBO
# ----------------------------

class ComboListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    queryset = Combo.objects.filter(is_active=True).prefetch_related("items__product__recipes__ingredient")
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ComboWithItemsSerializer
        return ComboSerializer

    def perform_create(self, serializer):
        combo = serializer.save()
        if is_neon_reachable(force=True):
            return

        items_payload = [
            {
                "product_id": str(row.product_id),
                "quantity": int(row.quantity),
            }
            for row in combo.items.all()
        ]
        _enqueue_catalog_create(
            "combo",
            {
                "id": str(combo.id),
                "name": combo.name,
                "price": str(combo.price),
                "gst_percent": str(combo.gst_percent),
                "is_active": bool(combo.is_active),
                "items": items_payload,
            },
            combo.id,
        )


class AddonRetrieveUpdateDeleteView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = Addon.objects.select_related("ingredient", "ingredient__category").all()
    serializer_class = AddonSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

class ComboRetrieveUpdateDeleteView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = Combo.objects.all().prefetch_related("items__product__recipes__ingredient")
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return ComboWithItemsSerializer
        return ComboSerializer


class ComboItemListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    serializer_class = ComboItemSerializer
    parser_classes = [JSONParser]
    queryset = ComboItem.objects.select_related("combo", "product").all()

    def get_queryset_for_alias(self, alias):
        queryset = self.queryset.using(alias)
        combo_id = self.request.query_params.get("combo")
        if combo_id:
            queryset = queryset.filter(combo_id=combo_id)
        return queryset

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class ComboItemRetrieveUpdateDeleteView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = ComboItem.objects.select_related("combo", "product").all()
    serializer_class = ComboItemSerializer
    parser_classes = [JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]


# ----------------------------
# BILLING CATALOG
# ----------------------------

class BillingCatalogView(APIView):
    permission_classes = [IsAdminOrStaff]

    @staticmethod
    def _should_prefer_sqlite():
        pending_catalog_statuses = ("PENDING", "IN_PROGRESS", "FAILED")
        return OfflineSyncQueue.objects.using("sqlite").filter(
            entity_type__in=("category", "product", "addon", "combo", "recipe"),
            status__in=pending_catalog_statuses,
        ).exists()

    def get(self, request):
        preferred_alias = (
            "sqlite"
            if self._should_prefer_sqlite()
            else ("neon" if is_neon_reachable(force=True) else "sqlite")
        )
        try:
            payload = build_billing_catalog_payload(db_alias=preferred_alias)
            return Response(payload, status=200)
        except (OperationalError, DatabaseError):
            payload = build_billing_catalog_payload(db_alias="sqlite")
            return Response(payload, status=200)


# ----------------------------
# RECIPE
# ----------------------------

class RecipeListCreateView(SQLiteFallbackQuerysetMixin, generics.ListCreateAPIView):

    queryset = Recipe.objects.select_related("product", "ingredient")
    serializer_class = RecipeSerializer

    def get_queryset_for_alias(self, alias):
        queryset = self.queryset.using(alias)
        product_id = self.request.query_params.get("product")
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        return queryset

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        recipe = serializer.save()
        if is_neon_reachable(force=False):
            return
        _enqueue_recipe_sync(
            "upsert",
            recipe.id,
            {
                "id": int(recipe.id),
                "product_id": str(recipe.product_id),
                "ingredient_id": str(recipe.ingredient_id),
                "quantity": str(recipe.quantity),
            },
        )

class RecipeUpdateDeleteView(SQLiteFallbackQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):

    queryset = Recipe.objects.select_related("product", "ingredient")
    serializer_class = RecipeSerializer
    permission_classes = [IsAdminRole]
    parser_classes = [JSONParser]

    def perform_update(self, serializer):
        recipe = serializer.save()
        if is_neon_reachable(force=False):
            return
        _enqueue_recipe_sync(
            "upsert",
            recipe.id,
            {
                "id": int(recipe.id),
                "product_id": str(recipe.product_id),
                "ingredient_id": str(recipe.ingredient_id),
                "quantity": str(recipe.quantity),
            },
        )

    def perform_destroy(self, instance):
        recipe_id = instance.id
        instance.delete()
        if is_neon_reachable(force=False):
            return
        _enqueue_recipe_sync(
            "delete",
            recipe_id,
            {"id": int(recipe_id)},
        )
