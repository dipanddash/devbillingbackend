from django.urls import path
from .views import (
    CategoryListCreateView,
    CategoryRetrieveUpdateDeleteView,
    ProductListCreateView,
    ProductUpdateView,
    AddonListCreateView,
    AddonRetrieveUpdateDeleteView,
    ComboListCreateView,
    ComboRetrieveUpdateDeleteView,
    ComboItemListCreateView,
    ComboItemRetrieveUpdateDeleteView,
    RecipeListCreateView,
    RecipeUpdateDeleteView,
    BillingCatalogView,
)

urlpatterns = [

    path(
        "categories/",
        CategoryListCreateView.as_view(),
        name="category-list-create"
    ),
    path(
        "categories/<uuid:pk>/",
        CategoryRetrieveUpdateDeleteView.as_view(),
        name="category-detail"
    ),
    path("products/", ProductListCreateView.as_view()),
    path("addons/", AddonListCreateView.as_view()),
    path("combos/", ComboListCreateView.as_view()),
    path("billing-catalog/", BillingCatalogView.as_view()),
    path("combo-items/", ComboItemListCreateView.as_view()),
    path("recipes/", RecipeListCreateView.as_view()),
    path("products/<uuid:pk>/", ProductUpdateView.as_view()),
    path("addons/<uuid:pk>/", AddonRetrieveUpdateDeleteView.as_view()),
    path("combos/<uuid:pk>/", ComboRetrieveUpdateDeleteView.as_view()),
    path("combo-items/<uuid:pk>/", ComboItemRetrieveUpdateDeleteView.as_view()),
    path("recipes/<int:pk>/", RecipeUpdateDeleteView.as_view()),

]
