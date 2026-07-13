"""
Section 03 — Custom Django Manager for automatic tenant scoping.

The TenantManager is the core of the multi-tenant isolation system. By overriding
get_queryset(), every ORM operation — .all(), .filter(), .get(), .count(),
.exists(), .update(), .delete() — automatically applies a tenant filter.

The design principle is "fail-safe over fail-open": when no tenant is set in
context, the manager returns queryset.none() rather than all records. This means
a forgotten middleware call results in an empty response, not a data leak.
"""

from django.db import models

from .context import get_current_tenant_id


class TenantManager(models.Manager):
    """
    Custom manager that automatically scopes all querysets to the current tenant.

    The current tenant is read from a contextvars.ContextVar set by
    TenantMiddleware at the start of each request (see context.py).

    Scoping is applied in get_queryset(), which is the single method Django
    calls for every ORM operation. This means:
    - There is no way to accidentally call .all() and get cross-tenant data.
    - Chained filters (.filter(status="confirmed")) still respect the scope.
    - Aggregations (.count(), .aggregate()) operate on scoped data.

    The only explicit escape hatches:
    - .unscoped() — returns a truly unscoped queryset for admin/system use.
      Requires a deliberate, visible call — not something a developer does
      accidentally.

    Fail-safe behaviour:
        If no tenant is in context (middleware did not run, management command,
        Celery task without tenant setup), get_queryset() returns queryset.none().
        This prevents data leaks in non-request contexts.
    """

    def get_queryset(self):
        """
        Override the base get_queryset to apply automatic tenant filtering.

        This method is called by Django for every ORM access on the manager:
        .all(), .filter(), .get(), .first(), .count(), .exists(), etc.

        Behaviour:
            - If a tenant_id is in context: filter queryset by that tenant_id.
            - If no tenant_id in context: return queryset.none() (fail-safe).

        Returns:
            QuerySet filtered to current tenant, or empty QuerySet if no tenant.
        """
        queryset = super().get_queryset()
        tenant_id = get_current_tenant_id()

        if tenant_id is not None:
            queryset = queryset.filter(tenant_id=tenant_id)
        else:
            # Fail-safe: return empty queryset when no tenant is set in context.
            # This prevents accidental cross-tenant data exposure in non-request
            # contexts (management commands, Celery tasks, shell, etc.).
            queryset = queryset.none()

        return queryset

    def unscoped(self):
        """
        Return an unscoped queryset that includes all tenants' records.

        This is the explicit escape hatch for admin operations, cross-tenant
        reporting, and system-level data access. Because it requires a
        deliberate .unscoped() call, it cannot be triggered accidentally by a
        developer writing normal business logic.

        Usage:
            # In Django admin
            def get_queryset(self, request):
                return TenantOrder.objects.unscoped()

        Returns:
            Unfiltered QuerySet containing all records regardless of tenant.
        """
        return super().get_queryset()

    def create(self, **kwargs):
        """
        Override create() to auto-assign the current tenant.

        Prevents the common mistake of forgetting to set `tenant` when creating
        a new object inside a request context. If the caller already provides
        `tenant` or `tenant_id`, their value is respected.

        Args:
            **kwargs: Model field values. If `tenant` and `tenant_id` are both
                      absent, the current context tenant_id is injected.

        Returns:
            The newly created model instance.
        """
        tenant_id = get_current_tenant_id()
        if tenant_id is not None and "tenant_id" not in kwargs and "tenant" not in kwargs:
            kwargs["tenant_id"] = tenant_id
        return super().create(**kwargs)

    def get_or_create(self, defaults=None, **kwargs):
        """
        Override get_or_create() to auto-assign the current tenant in lookup keys.

        The tenant_id is injected into the lookup kwargs so that the get()
        part of the operation is scoped to the current tenant, and any created
        record is automatically associated with it.

        Args:
            defaults: Dict of field values to set on creation (not used for lookup).
            **kwargs: Lookup fields. tenant_id is injected if absent.

        Returns:
            Tuple of (instance, created_bool).
        """
        tenant_id = get_current_tenant_id()
        if tenant_id is not None and "tenant_id" not in kwargs and "tenant" not in kwargs:
            kwargs["tenant_id"] = tenant_id
        return super().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        """
        Override update_or_create() to auto-assign the current tenant.

        Mirrors get_or_create() behaviour — ensures both the lookup and any
        created record are correctly scoped to the current tenant.

        Args:
            defaults: Dict of field values to set on creation or update.
            **kwargs: Lookup fields. tenant_id is injected if absent.

        Returns:
            Tuple of (instance, created_bool).
        """
        tenant_id = get_current_tenant_id()
        if tenant_id is not None and "tenant_id" not in kwargs and "tenant" not in kwargs:
            kwargs["tenant_id"] = tenant_id
        return super().update_or_create(defaults=defaults, **kwargs)


class UnscopedManager(models.Manager):
    """
    Manager that never applies tenant scoping.

    Use this as an additional manager on system-level models that don't belong
    to any tenant, or on Tenant itself. Unlike TenantManager.unscoped(), this
    manager can be assigned as the model's default manager for models that
    should always be unscoped.

    Example:
        class Tenant(models.Model):
            objects = UnscopedManager()
    """

    pass
