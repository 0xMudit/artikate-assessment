"""
Section 03 — Multi-Tenant Data Isolation: Models

Defines the SaaS data models. Each data model (TenantOrder, TenantProduct)
carries a ForeignKey to Tenant and uses TenantManager as its default manager.

The key design decision: tenant isolation is enforced at the *manager* level,
not the *view* level. This means it is impossible for a developer to accidentally
expose cross-tenant data by forgetting a .filter(tenant=...) call — the manager
always applies it automatically.

Model hierarchy:
    Tenant          — the client/organisation (no tenant scoping)
    TenantOrder     — scoped to current tenant via TenantManager
    TenantProduct   — scoped to current tenant via TenantManager
"""

from django.db import models

from .managers import TenantManager


class Tenant(models.Model):
    """
    Represents a client/organisation in the SaaS platform.

    This model is NOT tenant-scoped — it uses the default Django Manager.
    Tenant lookup happens in TenantMiddleware to resolve the incoming request
    to a specific tenant before setting the context variable.

    Fields:
        name:       Human-readable tenant name (e.g. "Acme Corp").
        subdomain:  Unique subdomain used for tenant resolution from the
                    HTTP Host header (e.g. "acme" → acme.example.com).
        is_active:  Soft-delete flag. Inactive tenants are rejected by middleware.
        created_at: Auto-set on creation — used for audit and ordering.
    """

    name = models.CharField(max_length=200)
    subdomain = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        """Return the tenant name."""
        return self.name


class TenantOrder(models.Model):
    """
    Order model with automatic tenant scoping via TenantManager.

    The custom manager (objects = TenantManager()) ensures:
    - TenantOrder.objects.all()    → only current tenant's orders
    - TenantOrder.objects.filter() → still scoped to current tenant
    - TenantOrder.objects.create() → auto-assigns current tenant

    The only way to bypass scoping is the explicit escape hatch:
    - TenantOrder.objects.unscoped() → all tenants (admin/system use only)

    Fail-safe: if no tenant is in context (no request middleware ran), the
    manager returns queryset.none() instead of all records. This prevents
    accidental data exposure in management commands, Celery tasks, or shell
    sessions that have not set a tenant context.

    Fields:
        tenant:        FK to Tenant — identifies which client owns this order.
        order_number:  Business-level identifier (e.g. "ACME-001"), unique per tenant.
        status:        Lifecycle state — pending → confirmed → shipped → delivered.
        total_amount:  Order total in decimal currency.
        customer_name: Denormalized customer name for display (no Customer FK in
                       this simplified model).
        created_at:    Auto-set on creation — used for ordering (newest first).
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="orders"
    )
    order_number = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    customer_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ["-created_at"]
        unique_together = ["tenant", "order_number"]  # Order numbers unique per tenant

    def __str__(self):
        """Return a label that includes the tenant name and order number."""
        return f"[{self.tenant.name}] Order {self.order_number}"


class TenantProduct(models.Model):
    """
    Product model with automatic tenant scoping via TenantManager.

    Same manager-level isolation as TenantOrder — TenantProduct.objects.all()
    only returns products belonging to the current tenant in context.

    The unique_together constraint on (tenant, sku) ensures SKUs are unique
    within a tenant's catalogue but different tenants can use the same SKU
    without conflict.

    Fields:
        tenant:  FK to Tenant.
        name:    Product display name.
        price:   Current selling price.
        sku:     Stock-keeping unit identifier, unique per tenant.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="products"
    )
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    sku = models.CharField(max_length=50)

    objects = TenantManager()

    class Meta:
        ordering = ["name"]
        unique_together = ["tenant", "sku"]

    def __str__(self):
        """Return a label that includes the tenant name and product name."""
        return f"[{self.tenant.name}] {self.name}"
