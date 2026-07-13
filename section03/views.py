"""
Section 03 — API views for tenant-scoped data.

These views are intentionally thin. All tenant isolation logic lives in:
  - TenantMiddleware  → resolves and binds the tenant to context
  - TenantManager     → filters every queryset to the current tenant

The views themselves do not need to call .filter(tenant=...) — that would be
redundant and would create a second place where tenant scoping could be missed.
The manager handles it unconditionally.

Endpoints:
  GET /api/tenants/orders/    — list orders for the current tenant
  GET /api/tenants/products/  — list products for the current tenant
"""

from rest_framework import generics

from .models import TenantOrder, TenantProduct
from .serializers import TenantOrderSerializer, TenantProductSerializer


class TenantOrderListView(generics.ListAPIView):
    """
    List all orders belonging to the current tenant.

    The queryset `TenantOrder.objects.all()` is automatically scoped by
    TenantManager to return only the orders whose `tenant_id` matches the
    tenant resolved by TenantMiddleware for this request.

    No explicit .filter(tenant=...) is needed here — that is the point of the
    manager-level scoping: developers write normal queryset code and isolation
    is guaranteed by the infrastructure, not developer discipline.

    If no tenant header is present in the request (TenantMiddleware sets
    request.tenant = None), TenantManager returns queryset.none() and the
    response is an empty list — the safe default.

    Returns:
        Paginated list of TenantOrderSerializer objects for the current tenant.
    """

    serializer_class = TenantOrderSerializer
    queryset = TenantOrder.objects.all()


class TenantProductListView(generics.ListAPIView):
    """
    List all products belonging to the current tenant.

    Same scoping mechanism as TenantOrderListView — the queryset is
    automatically filtered to the current tenant by TenantManager.
    Demonstrates that scoping works across multiple models without any
    additional view-level code.

    Returns:
        Paginated list of TenantProductSerializer objects for the current tenant.
    """

    serializer_class = TenantProductSerializer
    queryset = TenantProduct.objects.all()
