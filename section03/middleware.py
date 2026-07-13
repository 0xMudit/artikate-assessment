"""
Tenant middleware that extracts tenant from subdomain or JWT header
and binds it to the context for the full request lifecycle.

The middleware handles four tenant resolution strategies in priority order:
1. X-Tenant-ID header (primary — API clients, mobile apps, Postman)
2. JWT token in Authorization: Bearer header (extracts tenant_id claim)
3. Subdomain (secondary — web clients using acme.example.com routing)
4. ?tenant=<id> query parameter (fallback — development and test convenience)

Context lifecycle:
    process_request()  → resolves tenant → sets context via set_current_tenant_id()
    [view executes]    → TenantManager reads context → auto-scoped queries
    process_response() → clears context via set_current_tenant_id(None)

Cleanup guarantee:
    Even if the view raises an exception, Django's middleware machinery calls
    process_response() on the way out (for MiddlewareMixin subclasses). This
    ensures the tenant context is always cleaned up and never leaks between
    requests served by the same thread.
"""

import logging

import jwt
from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

from .context import set_current_tenant_id
from .models import Tenant

logger = logging.getLogger(__name__)


class TenantMiddleware(MiddlewareMixin):
    """
    Django middleware that resolves the current tenant and binds it to context.

    Inherits from MiddlewareMixin to support both WSGI and ASGI Django deployments
    (compatible with Django's synchronous and asynchronous middleware stacks).

    Resolution order:
        1. HTTP X-Tenant-ID header (configurable via TENANT_HEADER in settings)
        2. JWT token — decoded from Authorization: Bearer <token>, tenant_id
           extracted from the token payload (configurable claim name via
           TENANT_JWT_CLAIM in settings, defaults to "tenant_id")
        3. Subdomain extracted from the Host header
        4. ?tenant=<id> query parameter

    JWT handling:
        - Expects Authorization header with "Bearer <jwt>" format.
        - Decodes using settings.JWT_SECRET_KEY and settings.JWT_ALGORITHM.
        - Extracts tenant_id from the configured claim (default: "tenant_id").
        - On decode failure (expired, invalid signature), returns 401.
        - On missing tenant claim, falls through to next strategy.

    Error responses:
        400 Bad Request  — tenant ID provided but not a valid integer
        401 Unauthorized — JWT is present but invalid or expired
        404 Not Found    — valid integer but no active Tenant with that ID

    On success:
        - request.tenant is set to the Tenant model instance
        - set_current_tenant_id() is called so TenantManager can read it
    """

    def process_request(self, request):
        """
        Extract and validate the tenant for this request, then bind to context.

        Iterates through the resolution strategies in order. The first
        strategy that yields a tenant_id wins. If a tenant_id is found but
        does not correspond to an active Tenant, returns a 404 immediately.

        Args:
            request: Django HttpRequest object.

        Returns:
            None on success (middleware chain continues).
            JsonResponse(400) if the tenant header value is not a valid integer.
            JsonResponse(401) if the JWT is present but invalid/expired.
            JsonResponse(404) if the tenant does not exist or is inactive.
        """
        tenant_id = None

        # Strategy 1: X-Tenant-ID header (primary — explicit, machine-readable)
        header_name = getattr(settings, "TENANT_HEADER", "X-Tenant-ID")
        tenant_header = request.META.get(
            f"HTTP_{header_name.upper().replace('-', '_')}"
        )
        if tenant_header:
            try:
                tenant_id = int(tenant_header)
            except (ValueError, TypeError):
                return JsonResponse(
                    {"error": f"Invalid tenant ID: {tenant_header}"},
                    status=400,
                )

        # Strategy 2: JWT token from Authorization: Bearer <token>
        # Decodes the JWT and extracts the tenant_id claim from the payload.
        if tenant_id is None:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]  # strip "Bearer " prefix
                tenant_id = self._extract_tenant_from_jwt(token)
                if tenant_id == "INVALID":
                    return JsonResponse(
                        {"error": "Invalid or expired JWT token"},
                        status=401,
                    )

        # Strategy 3: Subdomain extraction from HTTP Host header
        # Matches acme.example.com → subdomain "acme" → Tenant lookup
        if tenant_id is None:
            host = request.get_host().split(":")[0]  # strip port if present
            parts = host.split(".")
            if len(parts) >= 3:  # must have at least subdomain.domain.tld
                subdomain = parts[0]
                try:
                    tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                    tenant_id = tenant.id
                except Tenant.DoesNotExist:
                    pass  # Subdomain not found — fall through to next strategy

        # Strategy 4: ?tenant=<id> query parameter (dev/testing convenience)
        if tenant_id is None:
            tenant_param = request.GET.get("tenant")
            if tenant_param:
                try:
                    tenant_id = int(tenant_param)
                except (ValueError, TypeError):
                    pass  # Invalid param — treat as no tenant provided

        # Validate the resolved tenant_id against the database
        if tenant_id is not None:
            try:
                tenant = Tenant.objects.get(id=tenant_id, is_active=True)
                request.tenant = tenant
                set_current_tenant_id(tenant_id)
            except Tenant.DoesNotExist:
                return JsonResponse(
                    {"error": f"Tenant {tenant_id} not found or inactive"},
                    status=404,
                )
        else:
            # No tenant resolved — request.tenant is None.
            # TenantManager will return queryset.none() for all queries (fail-safe).
            request.tenant = None

        return None  # Returning None means "continue to the next middleware/view"

    def _extract_tenant_from_jwt(self, token: str):
        """
        Decode a JWT and extract the tenant_id claim.

        Uses settings.JWT_SECRET_KEY for signature verification and
        settings.JWT_ALGORITHM (default HS256) for decoding.

        Args:
            token: Raw JWT string (without "Bearer " prefix).

        Returns:
            int: The tenant_id extracted from the token payload.
            "INVALID": If the token is expired, has an invalid signature,
                       or is missing the tenant_id claim. Signals the caller
                       to return a 401 response.
        """
        secret = getattr(settings, "JWT_SECRET_KEY", settings.SECRET_KEY)
        algorithm = getattr(settings, "JWT_ALGORITHM", "HS256")
        claim_name = getattr(settings, "TENANT_JWT_CLAIM", "tenant_id")

        try:
            payload = jwt.decode(token, secret, algorithms=[algorithm])
            tenant_id = payload.get(claim_name)
            if tenant_id is None:
                return None  # claim missing — fall through to next strategy
            return int(tenant_id)
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            return "INVALID"
        except jwt.InvalidSignatureError:
            logger.warning("JWT token has invalid signature")
            return "INVALID"
        except jwt.DecodeError:
            logger.warning("JWT token could not be decoded")
            return "INVALID"
        except (ValueError, TypeError):
            return "INVALID"

    def process_response(self, request, response):
        """
        Clean up tenant context after the response is built.

        Resetting the ContextVar to None ensures the current thread (or async
        context) does not carry a tenant value into the next request. Without
        this cleanup, a non-tenant request handled by the same thread after a
        tenant request might inherit the previous tenant's context.

        Args:
            request:  Django HttpRequest object.
            response: Django HttpResponse (or subclass) to be returned.

        Returns:
            The response unchanged (middleware does not modify the response body).
        """
        set_current_tenant_id(None)
        return response
