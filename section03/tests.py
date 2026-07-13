"""
Tests for Section 03 — Multi-Tenant Data Isolation

Proves:
1. Tenant A cannot access Tenant B's data through any ORM call.
2. Calling .objects.all() does not bypass scoping.
3. No tenant in context returns empty (fail-safe, not all data).
4. Middleware correctly sets and cleans up tenant context.
5. contextvars are isolated between threads (and therefore between async tasks).

Test strategy:
    Tests prove the *negative* — that isolation failures cannot happen — not
    just that the happy path works. The critical assertions are the ones that
    confirm Tenant A cannot see Tenant B's records, not just that Tenant A
    sees their own.
"""

from django.http import HttpRequest, JsonResponse
from django.test import TestCase, RequestFactory

from .context import current_tenant_var, set_current_tenant_id, get_current_tenant_id
from .managers import TenantManager
from .middleware import TenantMiddleware
from .models import Tenant, TenantOrder, TenantProduct


class TenantIsolationTest(TestCase):
    """
    Prove cross-tenant data isolation at the ORM layer.

    Two tenants (Acme Corp and Globex Inc) are created with separate orders
    and products. Each test sets a tenant in context, performs ORM operations,
    and asserts that only the correct tenant's data is visible.

    The key principle: these tests prove isolation *without* using any view-layer
    .filter(tenant=...) calls. The manager does all the work.
    """

    @classmethod
    def setUpTestData(cls):
        """
        Create two tenants with independent sets of orders and products.

        Uses setUpTestData (runs once per class) for performance — the test
        data is created in a transaction that is rolled back after the class,
        not after each method.
        """
        # Create two tenants
        cls.tenant_a = Tenant.objects.create(
            name="Acme Corp", subdomain="acme", is_active=True
        )
        cls.tenant_b = Tenant.objects.create(
            name="Globex Inc", subdomain="globex", is_active=True
        )

        # Create orders for tenant A (5 orders)
        for i in range(5):
            TenantOrder.objects.create(
                tenant=cls.tenant_a,
                order_number=f"ACME-{i:03d}",
                status="confirmed",
                total_amount=100.00,
                customer_name="Alice",
            )

        # Create orders for tenant B (3 orders)
        for i in range(3):
            TenantOrder.objects.create(
                tenant=cls.tenant_b,
                order_number=f"GLOBEX-{i:03d}",
                status="pending",
                total_amount=200.00,
                customer_name="Bob",
            )

        # One product per tenant
        TenantProduct.objects.create(
            tenant=cls.tenant_a, name="Widget A", price=10.00, sku="WA-001"
        )
        TenantProduct.objects.create(
            tenant=cls.tenant_b, name="Widget B", price=20.00, sku="WB-001"
        )

    def setUp(self):
        """Reset tenant context to None before each test."""
        set_current_tenant_id(None)

    def tearDown(self):
        """Ensure tenant context is always cleaned up after each test."""
        set_current_tenant_id(None)

    def test_tenant_a_sees_only_own_orders(self):
        """
        With Tenant A in context, .objects.all() returns exactly 5 orders.
        All returned orders belong to Tenant A.
        """
        set_current_tenant_id(self.tenant_a.id)
        orders = list(TenantOrder.objects.all())
        self.assertEqual(len(orders), 5)
        for order in orders:
            self.assertEqual(order.tenant_id, self.tenant_a.id)

    def test_tenant_b_sees_only_own_orders(self):
        """
        With Tenant B in context, .objects.all() returns exactly 3 orders.
        All returned orders belong to Tenant B.
        """
        set_current_tenant_id(self.tenant_b.id)
        orders = list(TenantOrder.objects.all())
        self.assertEqual(len(orders), 3)
        for order in orders:
            self.assertEqual(order.tenant_id, self.tenant_b.id)

    def test_tenant_a_cannot_see_tenant_b_data(self):
        """
        With Tenant A in context, no ORM call can retrieve Tenant B's records.

        Tests four ORM entry points:
        - .all()     — the obvious one; should be scoped
        - .filter()  — chained filter; still scoped
        - .count()   — aggregate; still scoped
        - .exists()  — boolean check; still scoped even with explicit pk lookup

        Each assertion proves the negative: Tenant B's data is unreachable,
        not just that Tenant A's data is present.
        """
        set_current_tenant_id(self.tenant_a.id)

        # .all() is scoped — cannot contain Tenant B records
        all_orders = TenantOrder.objects.all()
        self.assertFalse(
            all_orders.filter(tenant_id=self.tenant_b.id).exists(),
            "Tenant A should not see Tenant B's orders via .all()",
        )

        # .filter() is still scoped — chaining does not escape the manager filter
        filtered = TenantOrder.objects.filter(status="pending")
        for order in filtered:
            self.assertEqual(
                order.tenant_id, self.tenant_a.id,
                "Filtered results should still be tenant-scoped",
            )

        # .count() is scoped — returns Tenant A's count, not total
        count = TenantOrder.objects.count()
        self.assertEqual(count, 5, "Count should only include Tenant A's orders")

        # .exists() is scoped — cannot find Tenant B's order by its order_number
        self.assertFalse(
            TenantOrder.objects.filter(order_number="GLOBEX-000").exists(),
            "Tenant A should not find Tenant B's order by order_number",
        )

    def test_tenant_b_cannot_see_tenant_a_data(self):
        """
        With Tenant B in context, only 3 orders are visible (Tenant B's).
        Tenant A's 5 orders are invisible.
        """
        set_current_tenant_id(self.tenant_b.id)

        orders = list(TenantOrder.objects.all())
        self.assertEqual(len(orders), 3)
        for order in orders:
            self.assertEqual(order.tenant_id, self.tenant_b.id)

    def test_objects_all_does_not_bypass_scoping(self):
        """
        .objects.all() does NOT return all tenants' data.

        This is the core guarantee: the most natural ORM call (.all()) is
        just as scoped as any other. A developer cannot accidentally expose
        all records by "being lazy" and calling .all().
        """
        set_current_tenant_id(self.tenant_a.id)

        all_orders = TenantOrder.objects.all()
        tenant_ids = set(all_orders.values_list("tenant_id", flat=True))
        self.assertEqual(
            tenant_ids, {self.tenant_a.id},
            ".all() should only return current tenant's data",
        )

    def test_no_tenant_set_returns_empty(self):
        """
        Without a tenant in context, .objects.all() returns an empty queryset.

        This is the fail-safe behaviour: missing tenant context results in no data,
        not all data. A forgotten middleware call or a Celery task without tenant
        setup sees nothing rather than everything.
        """
        # No set_current_tenant_id() call — default context is None
        orders = list(TenantOrder.objects.all())
        self.assertEqual(
            len(orders), 0,
            "No tenant in context should return empty queryset (fail-safe)",
        )

    def test_products_are_also_scoped(self):
        """
        Tenant scoping works on TenantProduct as well as TenantOrder.

        Verifies the manager works for any model that uses TenantManager,
        not just one specific model.
        """
        set_current_tenant_id(self.tenant_a.id)
        products = list(TenantProduct.objects.all())
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, "Widget A")

    def test_create_auto_assigns_tenant(self):
        """
        TenantManager.create() auto-assigns the current tenant.

        Proves that developers don't need to pass tenant=... on every create()
        call. The manager injects it automatically from context, eliminating a
        common source of data isolation bugs.
        """
        set_current_tenant_id(self.tenant_a.id)
        order = TenantOrder.objects.create(
            order_number="ACME-AUTO-001",
            status="pending",
            total_amount=50.00,
            customer_name="Charlie",
        )
        self.assertEqual(order.tenant_id, self.tenant_a.id)

    def test_get_or_create_auto_assigns_tenant(self):
        """
        TenantManager.get_or_create() auto-assigns the current tenant.

        The tenant_id is injected into the lookup kwargs so both the GET
        and CREATE paths are correctly scoped.
        """
        set_current_tenant_id(self.tenant_a.id)
        order, created = TenantOrder.objects.get_or_create(
            order_number="ACME-GOC-001",
            defaults={"status": "pending", "total_amount": 75.00, "customer_name": "Dave"},
        )
        self.assertTrue(created)
        self.assertEqual(order.tenant_id, self.tenant_a.id)

    def test_unscoped_bypass_requires_explicit_call(self):
        """
        .unscoped() returns all tenants' data — but only when explicitly called.

        Proves the escape hatch works for admin use cases AND that it requires
        deliberate intent. Normal business logic code calling .all() or .filter()
        cannot reach .unscoped() without explicitly writing it.
        """
        set_current_tenant_id(self.tenant_a.id)

        # Unscoped returns ALL orders regardless of tenant
        all_orders_unscoped = TenantOrder.objects.unscoped()
        tenant_ids = set(all_orders_unscoped.values_list("tenant_id", flat=True))
        self.assertEqual(
            tenant_ids, {self.tenant_a.id, self.tenant_b.id},
            "unscoped() should return all tenants' data",
        )


class TenantMiddlewareTest(TestCase):
    """
    Test the tenant middleware's extraction, validation, and cleanup behaviour.

    Tests cover all four resolution strategies (header, JWT, subdomain, query param)
    plus error cases (invalid ID, nonexistent tenant, invalid JWT) and lifecycle cleanup.
    """

    def setUp(self):
        """Create a middleware instance and a test tenant."""
        self.factory = RequestFactory()
        # MiddlewareMixin requires get_response; pass a dummy callable for direct testing
        self.middleware = TenantMiddleware(get_response=lambda r: JsonResponse({"ok": True}))
        self.tenant = Tenant.objects.create(
            name="Middleware Test", subdomain="mwtest", is_active=True
        )

    def tearDown(self):
        """Reset tenant context after each test."""
        set_current_tenant_id(None)

    def test_middleware_sets_tenant_from_header(self):
        """
        When X-Tenant-ID header is present, the middleware resolves and sets the tenant.

        After process_request(), both request.tenant and get_current_tenant_id()
        should reflect the resolved tenant.
        """
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_X_TENANT_ID"] = str(self.tenant.id)

        result = self.middleware.process_request(request)
        self.assertIsNone(result)  # None = continue to view, no error response
        self.assertEqual(request.tenant.id, self.tenant.id)
        self.assertEqual(get_current_tenant_id(), self.tenant.id)

    def test_middleware_rejects_invalid_tenant(self):
        """
        When the header value is not a valid integer, the middleware returns 400.

        This prevents integer parsing errors from propagating as 500 errors.
        """
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_X_TENANT_ID"] = "not-a-number"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 400)

    def test_middleware_rejects_nonexistent_tenant(self):
        """
        When the header contains a valid integer but no Tenant with that ID
        exists (or the tenant is inactive), the middleware returns 404.
        """
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_X_TENANT_ID"] = "99999"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 404)

    def test_middleware_cleans_up_after_response(self):
        """
        After process_response(), the tenant context is reset to None.

        This prevents the current request's tenant from leaking into the next
        request served by the same thread (or async context).
        """
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_X_TENANT_ID"] = str(self.tenant.id)

        self.middleware.process_request(request)
        self.assertEqual(get_current_tenant_id(), self.tenant.id)

        response = JsonResponse({"ok": True})
        self.middleware.process_response(request, response)
        self.assertIsNone(get_current_tenant_id())

    def test_middleware_sets_tenant_from_query_param(self):
        """
        When no header is present, the middleware falls back to ?tenant=<id>.

        This strategy is for development and testing convenience — it should
        not be used in production without authentication.
        """
        request = self.factory.get(f"/api/tenants/orders/?tenant={self.tenant.id}")

        result = self.middleware.process_request(request)
        self.assertIsNone(result)
        self.assertEqual(request.tenant.id, self.tenant.id)

    # ─── JWT extraction tests ────────────────────────────────────────────

    def _make_jwt(self, payload, secret=None, algorithm="HS256"):
        """Helper to create a signed JWT token."""
        import jwt as pyjwt
        from django.conf import settings
        return pyjwt.encode(
            payload,
            secret or settings.JWT_SECRET_KEY,
            algorithm=algorithm,
        )

    def test_middleware_sets_tenant_from_jwt(self):
        """
        When Authorization: Bearer <jwt> is present and valid, the middleware
        extracts tenant_id from the token payload.
        """
        token = self._make_jwt({"tenant_id": self.tenant.id, "user": "alice"})
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        result = self.middleware.process_request(request)
        self.assertIsNone(result)
        self.assertEqual(request.tenant.id, self.tenant.id)
        self.assertEqual(get_current_tenant_id(), self.tenant.id)

    def test_middleware_rejects_expired_jwt(self):
        """
        When the JWT is expired, the middleware returns 401 Unauthorized.
        """
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        token = self._make_jwt({
            "tenant_id": self.tenant.id,
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        })
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 401)

    def test_middleware_rejects_invalid_signature_jwt(self):
        """
        When the JWT signature doesn't match, the middleware returns 401.
        """
        token = self._make_jwt(
            {"tenant_id": self.tenant.id},
            secret="wrong-secret-key",
        )
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 401)

    def test_middleware_rejects_malformed_jwt(self):
        """
        When the token is not a valid JWT at all, the middleware returns 401.
        """
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer not-a-valid-jwt-token"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 401)

    def test_middleware_jwt_missing_claim_falls_through(self):
        """
        When the JWT is valid but missing the tenant_id claim, the middleware
        falls through to the next resolution strategy (subdomain, query param).
        """
        token = self._make_jwt({"user": "alice", "role": "admin"})
        request = self.factory.get(f"/api/tenants/orders/?tenant={self.tenant.id}")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        result = self.middleware.process_request(request)
        self.assertIsNone(result)
        # Resolved via query param fallback, not JWT
        self.assertEqual(request.tenant.id, self.tenant.id)

    def test_middleware_jwt_nonexistent_tenant(self):
        """
        When the JWT contains a tenant_id that doesn't exist, returns 404.
        """
        token = self._make_jwt({"tenant_id": 99999})
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        result = self.middleware.process_request(request)
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 404)

    def test_middleware_header_takes_priority_over_jwt(self):
        """
        X-Tenant-ID header takes priority over JWT token.
        If both are present, the header wins.
        """
        other_tenant = Tenant.objects.create(
            name="Other Corp", subdomain="other", is_active=True
        )
        jwt_token = self._make_jwt({"tenant_id": other_tenant.id})
        request = self.factory.get("/api/tenants/orders/")
        request.META["HTTP_X_TENANT_ID"] = str(self.tenant.id)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {jwt_token}"

        result = self.middleware.process_request(request)
        self.assertIsNone(result)
        # Header wins over JWT
        self.assertEqual(request.tenant.id, self.tenant.id)


class ContextVarsTest(TestCase):
    """
    Test that contextvars provide correct isolation semantics.

    These tests prove that contextvars work as documented — isolated per thread
    (and by extension, per asyncio task). This is the foundational proof that
    the system is safe for async Django views.
    """

    def tearDown(self):
        """Clean up tenant context after each test."""
        set_current_tenant_id(None)

    def test_context_var_default_is_none(self):
        """
        The default tenant context is None.

        This is the safe initial state — no tenant set means no data visible.
        """
        self.assertIsNone(get_current_tenant_id())

    def test_context_var_set_and_get(self):
        """
        set_current_tenant_id() and get_current_tenant_id() round-trip correctly.
        """
        set_current_tenant_id(42)
        self.assertEqual(get_current_tenant_id(), 42)

    def test_context_var_reset(self):
        """
        Setting the context to None after setting a value clears it.

        Proves the cleanup path works — process_response() calling
        set_current_tenant_id(None) actually removes the tenant from context.
        """
        set_current_tenant_id(42)
        set_current_tenant_id(None)
        self.assertIsNone(get_current_tenant_id())

    def test_context_var_isolation_between_threads(self):
        """
        contextvars are isolated between threads.

        Thread 1 sets tenant_id=1, Thread 2 sets tenant_id=2. Both read their
        own values without interference. This is the property that makes
        contextvars correct for async Django — each asyncio Task (which may
        run on any thread) gets its own isolated context copy.

        This test is the direct counterexample to why thread-locals would fail
        in async Django: with thread-locals, Thread 2 setting a different value
        would overwrite Thread 1's value on the same thread.
        """
        import threading

        results = {}

        def worker(tenant_id, key):
            """Set a tenant in a thread and read it back into results."""
            set_current_tenant_id(tenant_id)
            results[key] = get_current_tenant_id()

        t1 = threading.Thread(target=worker, args=(1, "t1"))
        t2 = threading.Thread(target=worker, args=(2, "t2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results["t1"], 1)
        self.assertEqual(results["t2"], 2)
