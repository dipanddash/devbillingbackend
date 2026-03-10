from django.contrib import admin
from .models import Category, Product, Addon, ProductAddon, Recipe

admin.site.register(Category)
admin.site.register(Product)
admin.site.register(Addon)
admin.site.register(ProductAddon)
admin.site.register(Recipe)

from .models import Combo, ComboItem

class ComboItemInline(admin.TabularInline):
    model = ComboItem
    extra = 1


@admin.register(Combo)
class ComboAdmin(admin.ModelAdmin):

    list_display = ("name", "price", "is_active")

    inlines = [ComboItemInline]
