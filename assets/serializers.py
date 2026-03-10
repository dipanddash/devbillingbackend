from rest_framework import serializers

from .models import Asset, AssetCategory, AssetLog


class AssetCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetCategory
        fields = "__all__"


class AssetSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Asset
        fields = "__all__"


class AssetLogSerializer(serializers.ModelSerializer):
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    performed_by_username = serializers.CharField(
        source="performed_by.username",
        read_only=True
    )

    class Meta:
        model = AssetLog
        fields = "__all__"
