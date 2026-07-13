"""
Root URL configuration for the artikate-assessment project.

URL structure:
    /admin/              — Django admin (standard)
    /silk/               — Silk profiler dashboard (Section 01 evidence)
    /api/                — Section 01: order summary endpoints
    /api/queue/          — Section 02: email queue endpoints
    /api/tenants/        — Section 03: tenant-scoped data endpoints

Section routing:
    Each app owns its own urls.py and is included here with a prefix.
    This keeps the root URL conf minimal and delegates routing logic
    to the relevant app.

Silk dashboard:
    The /silk/ prefix exposes the Silk profiler UI. Accessible only when
    DEBUG=True (enforced by SILKY_INTERCEPT_FUNC in settings.py).
    Navigate to http://localhost:8000/silk/ to see per-request query counts
    and the before/after comparison for Section 01.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Django admin interface
    path("admin/", admin.site.urls),

    # Silk profiler UI — Section 01 query count evidence
    # Access at: http://localhost:8000/silk/
    path("silk/", include("silk.urls", namespace="silk")),

    # Section 01: /api/orders/summary/ (broken) and /api/orders/summary/fixed/ (fixed)
    path("api/", include("section01.urls")),

    # Section 02: /api/queue/send/, /api/queue/send-batch/, /api/queue/rate-status/
    path("api/queue/", include("section02.urls")),

    # Section 03: /api/tenants/orders/, /api/tenants/products/
    path("api/tenants/", include("section03.urls")),
]
