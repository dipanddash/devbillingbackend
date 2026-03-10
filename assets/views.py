from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import IsAdminRole

from .models import Asset, AssetCategory, AssetLog
from .serializers import (
    AssetSerializer,
    AssetCategorySerializer,
    AssetLogSerializer,
)


class AssetCategoryListCreateView(generics.ListCreateAPIView):
    queryset = AssetCategory.objects.all().order_by("name")
    serializer_class = AssetCategorySerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class AssetCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AssetCategory.objects.all()
    serializer_class = AssetCategorySerializer

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]


class AssetListCreateView(generics.ListCreateAPIView):
    queryset = Asset.objects.select_related("category").all().order_by("name")
    serializer_class = AssetSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]


class AssetDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Asset.objects.select_related("category").all()
    serializer_class = AssetSerializer

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsAdminRole()]
        return [IsAuthenticated()]


class AssetLogListView(generics.ListAPIView):
    queryset = AssetLog.objects.select_related("asset", "performed_by").all().order_by("-created_at")
    serializer_class = AssetLogSerializer
    permission_classes = [IsAuthenticated]
