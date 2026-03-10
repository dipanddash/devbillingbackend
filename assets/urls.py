from django.urls import path

from .views import (
    AssetCategoryDetailView,
    AssetCategoryListCreateView,
    AssetDetailView,
    AssetListCreateView,
    AssetLogListView,
)


urlpatterns = [
    path("categories/", AssetCategoryListCreateView.as_view()),
    path("categories/<int:pk>/", AssetCategoryDetailView.as_view()),
    path("", AssetListCreateView.as_view()),
    path("<uuid:pk>/", AssetDetailView.as_view()),
    path("logs/", AssetLogListView.as_view()),
]
