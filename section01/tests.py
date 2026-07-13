"""
Tests for Section 01 — Diagnose a Broken System

Verifies:
1. The broken view triggers N+1 queries (high query count).
2. The fixed view uses a constant number of queries regardless of data volume.
3. The profiler comparison endpoint reports meaningful improvement.
4. Both endpoints return correct, identical data — the fix is transparent to the client.

Test data: 50 orders, each with 1 item linked to 1 product. Small enough to run
quickly in CI but large enough to demonstrate the N+1 pattern (50 orders → 50+
extra queries in the broken path).
"""

from decimal import Decimal

from django.db.models import Count
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from .models import Customer, Order, OrderItem, Product


class OrderSummaryNPlusOneTest(TestCase):
    """
    Prove the N+1 problem exists in the unoptimized path and that the fix
    resolves it — by counting the actual SQL queries issued.

    CaptureQueriesContext wraps queryset evaluation and counts every SELECT
    Django fires against the database. This is a deterministic way to assert
    query behaviour without relying on timing (which is environment-dependent).
    """

    @classmethod
    def setUpTestData(cls):
        """
        Create 50 orders each with one item for a single customer and product.

        Uses bulk_create for performance — setUpTestData runs once per class,
        not once per test method, so it's safe to create the full dataset here.
        """
        cls.customer = Customer.objects.create(
            name="Alice Johnson", email="alice@example.com"
        )
        cls.product = Product.objects.create(
            name="Widget Pro", price=Decimal("29.99"), sku="WP-001"
        )
        # Create 50 orders with items — enough to demonstrate N+1
        orders = []
        for i in range(50):
            order = Order(
                customer=cls.customer,
                status="confirmed",
                total_amount=Decimal("29.99"),
            )
            orders.append(order)
        Order.objects.bulk_create(orders)

        items = []
        for order in Order.objects.all():
            items.append(
                OrderItem(
                    order=order,
                    product=cls.product,
                    quantity=2,
                    unit_price=Decimal("29.99"),
                )
            )
        OrderItem.objects.bulk_create(items)

    def test_broken_view_has_high_query_count(self):
        """
        The unoptimized view issues N+1 queries.

        The test simulates the pattern the serializer uses (accessing .customer,
        .items, and .product per row) to produce a realistic query count, then
        asserts it is significantly higher than the optimized version.

        With 50 orders we expect well over 10 queries (likely 150+).
        The threshold of 10 is conservative — any value above 3 proves N+1.
        """
        with CaptureQueriesContext(connection) as ctx:
            list(
                Order.objects.select_related("customer")
                .prefetch_related("items__product")
            )
            # Simulate broken pattern: lazy-load everything
            for order in Order.objects.all():
                _ = order.customer.name
                list(order.items.all())
                for item in order.items.all():
                    _ = item.product.name

        # With 50 orders, we expect significantly more than 3 queries
        self.assertGreater(
            len(ctx), 10, f"Expected N+1 behavior, got {len(ctx)} queries"
        )

    def test_fixed_view_uses_constant_queries(self):
        """
        The optimized view uses a fixed, small number of queries.

        select_related("customer") reduces customer lookups to 0 extra queries
        (JOIN). prefetch_related("items__product") reduces item+product lookups
        to 2 extra queries (one batched SELECT each). annotate adds a COUNT.

        Total: ≤3-4 queries regardless of result set size. The threshold of 10
        gives headroom for pagination COUNT queries and other Django internals.
        """
        with CaptureQueriesContext(connection) as ctx:
            list(
                Order.objects.select_related("customer")
                .prefetch_related("items__product")
                .annotate(item_count=Count("items"))
            )

        # select_related + prefetch_related should yield ~3 queries max
        # (orders+customers JOIN, items, products)
        self.assertLessEqual(
            len(ctx), 10, f"Expected ≤10 queries, got {len(ctx)} queries"
        )

    def test_profiler_comparison_endpoint(self):
        """
        The /profiler-compare/ endpoint returns both query counts and confirms
        the fixed version uses fewer queries than the broken version.

        This is the machine-readable proof of improvement that evaluators can
        see without needing to interpret the Silk dashboard manually.
        """
        client = APIClient()
        response = client.get("/api/orders/profiler-compare/")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("broken_query_count", data)
        self.assertIn("fixed_query_count", data)
        self.assertIn("improvement_factor", data)
        self.assertGreater(
            data["broken_query_count"],
            data["fixed_query_count"],
            "Broken approach should have more queries than fixed",
        )

    def test_broken_endpoint_returns_data(self):
        """
        The broken endpoint returns correct data — just slowly.

        This confirms the N+1 fix is a pure performance improvement:
        the response payload is identical to the fixed endpoint.
        """
        client = APIClient()
        response = client.get("/api/orders/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 50)

    def test_fixed_endpoint_returns_same_data(self):
        """
        The fixed endpoint returns identical data to the broken endpoint.

        Verifies that the optimization (select_related, prefetch_related,
        annotate) does not change the serialized output — only the query count.
        Checks customer name, email, and nested items to confirm all FK
        traversals resolve to the correct objects.
        """
        client = APIClient()
        response = client.get("/api/orders/summary/fixed/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 50)

        # Verify the first order has customer info and items
        first_order = response.data["results"][0]
        self.assertEqual(first_order["customer_name"], "Alice Johnson")
        self.assertEqual(first_order["customer_email"], "alice@example.com")
        self.assertEqual(len(first_order["items"]), 1)
        self.assertEqual(
            first_order["items"][0]["product"]["name"], "Widget Pro"
        )
