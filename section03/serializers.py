"""
Section 03 — Serializers for tenant-scoped models.

These serializers present the tenant-scoped models to the API layer.
The `tenant` field is marked read-only on all writable serializers —
it is set automatically by TenantManager.create(), not by the caller.
This prevents a client from submitting data for a different tenant's ID.
"""

from rest_framework import serializers

from .models import Tenant, TenantOrder, TenantProduct


class TenantSerializer(serializers.ModelSerializer):
    """
    Serializes a Tenant object for reference responses.

    Used in admin-level views or debugging. Not used in the main
    tenant-scoped endpoints (where the tenant is implicit in context).
    """

    class Meta:
        model = Tenant
        fields = ["id", "name", "subdomain", "is_active", "created_at"]


class TenantOrderSerializer(serializers.ModelSerializer):
    """
    Serializes a TenantOrder for the orders list endpoint.

    `tenant_name` is a read-only denormalized field sourced from the FK
    relationship. Because TenantOrderListView uses the default queryset
    (TenantOrder.objects.all()), the tenant FK is already scoped — but
    select_related("tenant") should be added to the view's queryset in
    production to avoid a per-row query for tenant.name.

    The `tenant` field itself is excluded from the output — the client already
    knows which tenant they are authenticated as (via the request header), so
    repeating the tenant ID in every row is redundant.

    `read_only_fields = ["tenant"]` ensures that even if the client submits a
    tenant value in a POST/PATCH body, it is silently ignored. The correct
    tenant is always set by TenantManager.create() from context.
    """

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = TenantOrder
        fields = [
            "id",
            "tenant_name",
            "order_number",
            "status",
            "total_amount",
            "customer_name",
            "created_at",
        ]
        read_only_fields = ["tenant"]


class TenantProductSerializer(serializers.ModelSerializer):
    """
    Serializes a TenantProduct for the products list endpoint.

    Simple flat serializer — no nested relationships. The `tenant` field is
    excluded from output and marked read-only to prevent tenant spoofing in
    write requests. The scoping is guaranteed at the manager layer, not here.
    """

    class Meta:
        model = TenantProduct
        fields = ["id", "name", "price", "sku"]
        read_only_fields = ["tenant"]
