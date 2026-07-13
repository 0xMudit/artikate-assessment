"""
Tenant context management using Python's contextvars.

WHY contextvars instead of thread-locals
=========================================
Python's `threading.local()` stores data per OS thread. In synchronous Django
(WSGI), each request occupies one thread for its entire lifecycle, so thread-locals
safely isolate request-scoped data.

In async Django (ASGI), a single thread can handle *multiple concurrent requests*
via cooperative multitasking (asyncio event loop). If Request A sets:
    threading.local().tenant_id = 1
...and Request B (sharing the same thread) reads it before A's middleware cleans
up, Request B sees Request A's tenant — a critical data leak between clients.

`contextvars.ContextVar` (Python 3.7+) stores data per *execution context*, not
per thread. When an asyncio.Task is created, it automatically copies the current
context (a shallow copy). This means:
- Request A's ContextVar is isolated from Request B's, even on the same thread.
- The value propagates correctly through `await` chains *within* a single request.
- Cleanup is explicit — the middleware resets the ContextVar at the end of each
  request via process_response().

This module is the single source of truth for tenant context state.
"""

import contextvars

# The ContextVar that holds the current tenant ID for the request lifecycle.
#
# Default is None — meaning "no tenant set" — which causes TenantManager to
# return an empty queryset (fail-safe behaviour).
#
# Type annotation: int | None (tenant primary key or None)
current_tenant_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tenant", default=None
)


def get_current_tenant_id() -> int | None:
    """
    Retrieve the current tenant ID from the active execution context.

    Returns:
        The integer tenant primary key set by TenantMiddleware for this request,
        or None if no tenant has been set (e.g. management commands, Celery tasks
        without tenant context, or before middleware runs).
    """
    return current_tenant_var.get()


def set_current_tenant_id(tenant_id: int | None) -> None:
    """
    Set the current tenant ID in the active execution context.

    Called by TenantMiddleware.process_request() at the start of each request
    (after validating the tenant exists and is active), and again with None in
    process_response() to clean up after the request completes.

    Can also be called directly in tests to set up tenant context without
    going through the full middleware stack.

    Args:
        tenant_id: Integer primary key of the tenant to activate, or None to
                   clear the context (makes TenantManager return empty querysets).
    """
    current_tenant_var.set(tenant_id)
