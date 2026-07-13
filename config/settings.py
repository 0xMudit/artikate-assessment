"""
Django settings for the artikate-assessment project.

This is a single-file settings module for local development. It is intentionally
simple — no environment-split (dev/staging/prod), no secrets management, no
per-environment overrides. This is appropriate for an assessment submission.

Key configuration decisions:
  - SQLite: Zero-config database. Adequate for assessment; production would use PostgreSQL.
  - Redis: Required for Section 02 (Celery broker + rate limiter). Default: localhost:6379.
  - Silk: Profiler middleware for Section 01 query count evidence.
  - TenantMiddleware: Loaded last so it has access to session/auth context.
  - CELERY_TASK_ACKS_LATE + REJECT_ON_WORKER_LOST: Critical for Section 02 crash safety.

Security note:
  SECRET_KEY is a placeholder dev key. ALLOWED_HOSTS = ["*"] is acceptable for
  local assessment runs but must never be used in production.
"""

import os
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Security ─────────────────────────────────────────────────────────────────
# WARNING: These values are not production-safe.
# SECRET_KEY must be replaced with a long random string before any deployment.
# DEBUG must be False in production to prevent traceback leakage.
# ALLOWED_HOSTS must list only the actual domain(s) in production.

SECRET_KEY = "django-insecure-assessment-dev-key-not-for-production"

DEBUG = True

ALLOWED_HOSTS = ["*"]

# ─── Installed Applications ───────────────────────────────────────────────────
# Standard Django apps + DRF + Silk profiler + CORS + the three assessment sections.

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "silk",             # Section 01: query profiler (django-silk)
    "corsheaders",      # CORS headers for API access from different origins
    "section01",        # Diagnose a Broken System (N+1 query demo)
    "section02",        # Rate-Limited Async Job Queue (Celery + Redis)
    "section03",        # Multi-Tenant Data Isolation (TenantManager + contextvars)
]

# ─── Middleware ────────────────────────────────────────────────────────────────
# Order matters. SilkyMiddleware must be early to capture all queries.
# TenantMiddleware is last in application middleware so it runs after auth/session.

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",        # CORS — must be before CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "silk.middleware.SilkyMiddleware",              # Section 01: captures query profiles
    "section03.middleware.TenantMiddleware",        # Section 03: binds tenant to context
]

ROOT_URLCONF = "config.urls"

# ─── Templates ────────────────────────────────────────────────────────────────

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ─── Database ─────────────────────────────────────────────────────────────────
# SQLite for local development and assessment. No additional setup required.
# Production equivalent: PostgreSQL with connection pooling (e.g. PgBouncer).

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# ─── Auth ─────────────────────────────────────────────────────────────────────
# Empty validators — assessment does not exercise user auth flows.

AUTH_PASSWORD_VALIDATORS = []

# ─── Internationalisation ─────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ─── Static Files ─────────────────────────────────────────────────────────────

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Django REST Framework ────────────────────────────────────────────────────
# PageNumberPagination with PAGE_SIZE=50 keeps list responses manageable.
# Tests assert on response.data["results"] which is the paginated wrapper key.

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# ─── Celery Configuration (Section 02) ────────────────────────────────────────
# Both the broker and result backend use Redis. Default to localhost for local dev.
# Override via environment variables for CI or container environments.
#
# CELERY_TASK_ACKS_LATE:
#   True = acknowledge tasks AFTER execution, not on pickup.
#   Combined with REJECT_ON_WORKER_LOST, this guarantees at-least-once delivery
#   even if the worker process is SIGKILL'd mid-execution.
#
# CELERY_TASK_REJECT_ON_WORKER_LOST:
#   True = if the worker process dies while running a task, the task message
#   is rejected (not acknowledged) and requeued by Redis for another worker.

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"        # JSON for human-readability during debugging
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_ACKS_LATE = True           # At-least-once delivery guarantee
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # Requeue on worker crash

# ─── Redis (Section 02 Rate Limiter) ──────────────────────────────────────────
# Separate DB index from Celery broker (index 0) to avoid key collisions.

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")

# ─── Silk Profiler Configuration (Section 01) ─────────────────────────────────
# Silk captures SQL queries per request and exposes them at /silk/.
# SILKY_PYTHON_PROFILER enables cProfile integration for CPU profiling.
# SILKY_INTERCEPT_FUNC restricts profiling to DEBUG mode only.

SILKY_PYTHON_PROFILER = True
SILKY_PYTHON_PROFILER_BINARY = True
SILKY_PYTHON_PROFILER_RECORD_REQUEST_TIME = True
SILKY_META = True
SILKY_INTERCEPT_FUNC = lambda request: DEBUG  # noqa: E731 — only profile in DEBUG mode

# ─── CORS ─────────────────────────────────────────────────────────────────────
# Allow all origins for local development. Restrict to specific domains in production.

CORS_ALLOW_ALL_ORIGINS = True

# ─── Section 03: Tenant Settings ──────────────────────────────────────────────
# The header name TenantMiddleware reads for tenant resolution.
# Can be changed to "Authorization" + JWT parsing in production.

TENANT_HEADER = "X-Tenant-ID"

# JWT configuration for tenant extraction from Bearer tokens.
# JWT_SECRET_KEY defaults to SECRET_KEY for development.
# In production, use a separate signing key and set JWT_ALGORITHM to RS256.

JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
TENANT_JWT_CLAIM = os.environ.get("TENANT_JWT_CLAIM", "tenant_id")
