from django.contrib import admin
from .models import Asset, AssetCategory, AssetLog
# Register your models here.
admin.site.register(Asset)
admin.site.register(AssetCategory)
admin.site.register(AssetLog)
