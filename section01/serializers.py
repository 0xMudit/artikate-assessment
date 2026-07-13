"""
Section 01 — Serializers

These serializers define the API output shape for the order summary endpoint.
They are also the primary *cause* of the N+1 problem in the broken view:

- OrderSummarySerializer accesses `customer.name` and `customer.email` via
  `source=` lookups — each triggers a lazy FK query if the customer is not
  already JOINed via select_related.
- OrderItemSerializer accesses `product.name` and `product.price` via nested
  ProductSerializer — triggers a lazy query per item if product is not prefetched.

The fix (select_related + prefetch_related in the view queryset) makes these
traversals free because the data is already in memory.
"""

from rest_framework import serializers

from .models import Customer, Order, OrderItem, Product


class ProductSerializer(serializers.ModelSerializer):
    """
    Serializes a Product for use inside order item responses.

    Read-only — products are not created or updated through the order API.
    Included fields are limited to what the order summary dashboard needs.
    """

    class Meta:
        model = Product
        fields = ["id", "name", "price", "sku"]


class OrderItemSerializer(serializers.ModelSerializer):
    """
    Serializes an individual order line item, including the nested product.

    The nested `product` field triggers a SELECT on the Product table for each
    item unless `prefetch_related("items__product")` or
    `select_related("product")` is used on the OrderItem queryset.

    `line_total` is a read-only computed field derived from `@property` on the
    model (quantity × unit_price) — no extra query, uses already-loaded data.
    """

    product = ProductSerializer(read_only=True)
    line_total = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price", "line_total"]


class OrderSummarySerializer(serializers.ModelSerializer):
    """
    Serializes an Order with denormalized customer fields and nested items.

    This serializer intentionally crosses two FK relationships:
      - customer.name via `source="customer.name"` → triggers lazy load without
        select_related("customer") on the queryset.
      - customer.email via `source="customer.email"` → same FK, cached after
        first access within the same request cycle.
      - items via `OrderItemSerializer(many=True)` → triggers a separate query
        per order unless prefetch_related("items") is used.

    `item_count` is populated from a queryset annotation
    (`annotate(item_count=Count("items"))`), making it a single SQL COUNT
    rather than a Python-level len() call after prefetch.
    """

    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_email = serializers.CharField(source="customer.email", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)
    item_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "customer_name",
            "customer_email",
            "status",
            "total_amount",
            "item_count",
            "items",
            "created_at",
        ]


class CustomerSerializer(serializers.ModelSerializer):
    """
    Serializes a Customer with an annotated order count.

    `order_count` is populated by an annotation on the queryset
    (`annotate(order_count=Count("orders"))`). This means the count is computed
    as a single SQL COUNT per row rather than issuing N queries — one per customer.
    """

    order_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Customer
        fields = ["id", "name", "email", "order_count", "created_at"]
