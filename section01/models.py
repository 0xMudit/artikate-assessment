"""
Section 01 — Models

Defines the core e-commerce domain models used to demonstrate the N+1 query
problem and its fix. The relationships between these models (Customer → Order →
OrderItem → Product) are what make the lazy-loading issue visible — the deeper
the traversal, the more queries the ORM issues without select_related/prefetch_related.
"""

from django.db import models


class Customer(models.Model):
    """
    Represents a customer who can place orders.

    The `name` and `email` fields are accessed by the OrderSummarySerializer
    via `source="customer.name"` and `source="customer.email"`. Without
    select_related("customer") on the queryset, each access triggers a separate
    SQL SELECT — the root cause of the N+1 problem in the broken view.

    Ordered by name alphabetically for predictable list output.
    """

    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        """Return the customer's display name."""
        return self.name


class Product(models.Model):
    """
    Represents a product that can be included in order items.

    Each OrderItem holds a FK to Product. Without prefetch_related("items__product"),
    the serializer triggers a SELECT for each item's product — adding O(N*M) queries
    for N orders with M items each.

    The `sku` field is unique to support idempotent lookups in tests and seed data.
    """

    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    sku = models.CharField(max_length=50, unique=True)

    def __str__(self):
        """Return the product name."""
        return self.name


class Order(models.Model):
    """
    Represents a customer's order, with a status lifecycle and a total amount.

    This is the central model for the N+1 demonstration. The view fetches a
    queryset of Orders, and the serializer accesses:
      - order.customer.name (requires JOIN or extra query per row)
      - order.customer.email (same FK access — cached after first access)
      - order.items (reverse FK — requires one query per order unless prefetched)

    Ordered newest-first so the profiler captures realistic dashboard behaviour.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="orders"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        """Return a human-readable label including order ID and customer name."""
        return f"Order #{self.pk} - {self.customer.name}"


class OrderItem(models.Model):
    """
    Represents a single line item within an Order.

    Links Order → Product with quantity and unit_price captured at order time
    (so later product price changes don't affect historical orders).

    The `line_total` property is a computed field used by OrderItemSerializer.
    It is computed in Python rather than the database, which is acceptable for
    small item counts per order.
    """

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        """Return a descriptive line-item label."""
        return f"{self.quantity}x {self.product.name} in Order #{self.order_id}"

    @property
    def line_total(self):
        """
        Calculate the total cost for this line item (quantity × unit_price).

        Computed in Python using the already-loaded product. If product is not
        prefetched, this triggers an extra query per item — another source of N+1.
        """
        return self.quantity * self.unit_price
