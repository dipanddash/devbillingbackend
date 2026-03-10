from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from accounts.permissions import IsAdminOrStaff, IsAdminRole

from .models import Category, Product, Recipe, Addon, Combo, ComboItem
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

# ----------------------------
# CATEGORY
# ----------------------------

class CategoryListCreateView(generics.ListCreateAPIView):

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class CategoryRetrieveUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    parser_classes = [MultiPartParser, FormParser]

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

class ProductListCreateView(generics.ListCreateAPIView):

    queryset = Product.objects.filter(is_active=True).select_related("category").prefetch_related("recipes__ingredient")
    serializer_class = ProductSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAdminOrStaff()]

    def perform_create(self, serializer):
        serializer.save(is_active=True)


class ProductUpdateView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAdminRole]

# ----------------------------
# ADDON
# ----------------------------

class AddonListCreateView(generics.ListCreateAPIView):

    queryset = Addon.objects.all()
    serializer_class = AddonSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

# ----------------------------
# COMBO
# ----------------------------

class ComboListCreateView(generics.ListCreateAPIView):

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


class AddonRetrieveUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Addon.objects.all()
    serializer_class = AddonSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]

class ComboRetrieveUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

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


class ComboItemListCreateView(generics.ListCreateAPIView):

    serializer_class = ComboItemSerializer
    parser_classes = [JSONParser]

    def get_queryset(self):
        queryset = ComboItem.objects.select_related("combo", "product").all()
        combo_id = self.request.query_params.get("combo")
        if combo_id:
            queryset = queryset.filter(combo_id=combo_id)
        return queryset

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class ComboItemRetrieveUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

    queryset = ComboItem.objects.select_related("combo", "product").all()
    serializer_class = ComboItemSerializer
    parser_classes = [JSONParser]

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]


# ----------------------------
# RECIPE
# ----------------------------

class RecipeListCreateView(generics.ListCreateAPIView):

    serializer_class = RecipeSerializer

    def get_queryset(self):
        queryset = Recipe.objects.select_related("product", "ingredient")

        product_id = self.request.query_params.get("product")

        if product_id:
            queryset = queryset.filter(product_id=product_id)

        return queryset

class RecipeUpdateDeleteView(generics.RetrieveUpdateDestroyAPIView):

    queryset = Recipe.objects.select_related("product", "ingredient")
    serializer_class = RecipeSerializer
    permission_classes = [IsAdminRole]
    parser_classes = [JSONParser]
