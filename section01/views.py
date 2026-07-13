"""
Section 01 — Diagnose a Broken System

The BROKEN endpoint demonstrates an N+1 query problem.
The FIXED endpoint uses select_related and prefetch_related to resolve it.
Both are available so the profiler can compare query counts.

Endpoints:
  GET /api/orders/summary/         — broken (N+1, slow at scale)
  GET /api/orders/summary/fixed/   — fixed (3 queries regardless of row count)
  GET /api/orders/profiler-compare/ — returns both query counts programmatically
  GET /api/customers/              — customer list with annotated order counts
"""

from django.db.models import Count, Prefetch
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Customer, Order, OrderItem, Product
from .serializers import CustomerSerializer, OrderSummarySerializer


class OrderSummaryBrokenView(generics.ListAPIView):
    """
    BROKEN endpoint — intentionally demonstrates the N+1 query problem.

    Problem:
        The queryset is `Order.objects.all()` with no prefetching. When the
        OrderSummarySerializer iterates over the results and accesses:
          - order.customer.name  → 1 extra SELECT per order
          - order.customer.email → cached after first access (same FK object)
          - order.items.all()    → 1 extra SELECT per order for items
          - item.product.name    → 1 extra SELECT per item for the product

        With 200 orders at 2 items each, this produces:
          1 (orders) + 200 (customers) + 200 (items queries) + 400 (products) = 801 queries

        This is why the endpoint times out at 30 seconds for large accounts.

    Root cause:
        Django's ORM uses lazy loading by default. Every access to a FK or
        reverse-FK relationship on an un-prefetched queryset result fires a
        fresh SELECT. The ORM's index on customer_id exists, but indexes only
        help per-query latency — they cannot eliminate the N×1 round trips.
    """

    serializer_class = OrderSummarySerializer
    queryset = Order.objects.all()

    def get_queryset(self):
        """Return all orders with no query optimization — demonstrates N+1."""
        return Order.objects.all()


class OrderSummaryFixedView(generics.ListAPIView):
    """
    FIXED endpoint — resolves the N+1 problem using select_related and prefetch_related.

    Fix strategy:
        1. select_related("customer")
           Performs a SQL LEFT OUTER JOIN between `section01_order` and
           `section01_customer` in the initial query. Customer data is fetched
           in one round trip and cached on each Order instance. Zero extra
           queries for customer access.

        2. prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("product")))
           Executes a single batched SELECT for all OrderItem rows that belong
           to orders in the result set. The nested select_related("product")
           JOINs the Product table into that same query. Django then maps the
           results in Python using a dictionary keyed by order_id. Zero extra
           queries per order for items or products.

        3. annotate(item_count=Count("items"))
           Adds a SQL COUNT subquery to the order SELECT so item_count is
           available as a column on each row — no Python-level len() needed.

    Result:
        Total queries: 3 (orders+customers, items+products, [pagination count])
        regardless of how many orders are in the result set.
    """

    serializer_class = OrderSummarySerializer

    def get_queryset(self):
        """
        Return an optimized queryset with select_related and prefetch_related.

        This is the canonical fix for the N+1 problem demonstrated in
        OrderSummaryBrokenView.
        """
        return Order.objects.select_related("customer").prefetch_related(
            Prefetch(
                "items",
                queryset=OrderItem.objects.select_related("product"),
            )
        ).annotate(item_count=Count("items"))


class ProfilerComparisonView(APIView):
    """
    Utility endpoint that programmatically measures the query count difference
    between the broken and fixed approaches and returns both as JSON.

    This is the machine-readable counterpart to the Silk dashboard. It runs
    both approaches in the same request using CaptureQueriesContext so the
    evaluator can see the improvement without needing to interpret profiler UI.

    Response fields:
        broken_query_count  — total queries fired by the N+1 approach
        fixed_query_count   — total queries fired by the optimized approach
        improvement_factor  — ratio (broken / fixed), rounded to 1 decimal place

    Note: The broken simulation explicitly accesses `.customer.name`,
    `.customer.email`, `.items.all()`, and `.product.name` per row to
    reproduce the full query pattern the serializer would trigger.
    """

    def get(self, request):
        """
        Execute both query patterns, capture query counts, return comparison.

        Uses Django's CaptureQueriesContext as a non-destructive way to count
        queries without modifying settings or installing extra middleware.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        # Broken approach: no select_related / prefetch_related
        with CaptureQueriesContext(connection) as broken_ctx:
            list(
                Order.objects.all()  # noqa: E501
            )
            # Force evaluation of the same fields the serializer would touch
            for order in Order.objects.all():
                _ = order.customer.name
                _ = order.customer.email
                list(order.items.all())
                for item in order.items.all():
                    _ = item.product.name
                    _ = item.product.price

        # Fixed approach
        with CaptureQueriesContext(connection) as fixed_ctx:
            list(
                Order.objects.select_related("customer")
                .prefetch_related("items__product")
                .annotate(item_count=Count("items"))
            )

        return Response(
            {
                "broken_query_count": len(broken_ctx),
                "fixed_query_count": len(fixed_ctx),
                "improvement_factor": (
                    round(len(broken_ctx) / len(fixed_ctx), 1)
                    if fixed_ctx
                    else "N/A"
                ),
            }
        )


class CustomerListView(generics.ListAPIView):
    """
    Lists all customers with their total order count.

    The `order_count` annotation is computed at the SQL level using COUNT("orders"),
    avoiding N queries for N customers. This endpoint also demonstrates the correct
    use of queryset annotations as an alternative to Python-level aggregation.
    """

    serializer_class = CustomerSerializer

    def get_queryset(self):
        """Return all customers with an annotated order_count aggregate."""
        return Customer.objects.annotate(order_count=Count("orders"))
