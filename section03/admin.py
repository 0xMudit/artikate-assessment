from django.contrib import admin

from .models import Tenant, TenantOrder, TenantProduct


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "subdomain", "is_active", "created_at"]
    list_filter = ["is_active"]


@admin.register(TenantOrder)
class TenantOrderAdmin(admin.ModelAdmin):
    list_display = ["order_number", "tenant", "status", "total_amount", "created_at"]
    list_filter = ["status", "tenant"]
    # Note: In production, the TenantManager would scope this admin
    # to show only the current tenant's orders. For superadmin access,
    # use TenantOrder.objects.unscoped() in a custom queryset.
