# ANSWERS.md — Written Responses

## Section 01 — Incident Investigation Log

### Investigation Narrative

**Step 1: Reproduce the issue.** I hit `/api/orders/summary/` with a user who has 200+ orders and confirmed the 30-second timeout. Under normal load (~80ms), this suggests a regression proportional to data volume — a classic sign of N+1 queries or missing indexes.

**Step 2: Check the database query log.** I enabled `django.db.backends` logging and counted the queries for a 200-order user. The log showed 200+ SELECT statements against the `section01_customer` and `section01_orderitem` tables — one per order for the customer lookup, and one per order for the items. This is the N+1 pattern.

**Step 3: Inspect the serializer.** The `OrderSummarySerializer` uses `source="customer.name"` and `source="customer.email"`, which triggers a separate query per order to fetch the related customer. The `items` field uses a nested `OrderItemSerializer`, which triggers another query per order to fetch items. Each item then accesses `product.name`, triggering yet another query.

**Step 4: Check for missing indexes.** The `customer_id` foreign key on the Order model already has a database index (Django creates one automatically for ForeignKey fields). The index exists — the problem is that the ORM is not using JOINs to leverage it.

**Step 5: Check the view's queryset.** The broken view returns `Order.objects.all()` with no `select_related` or `prefetch_related`. This means every relationship access triggers a lazy-loaded query.

### Root Cause Category: N+1 Query

The root cause is an **N+1 query problem** caused by lazy-loading of related objects in the Django ORM. The view returns `Order.objects.all()`, and the serializer accesses `order.customer.name`, `order.customer.email`, and iterates over `order.items` — each triggering a separate SQL query. With 200 orders, this produces 1 (base) + 200 (customer lookups) + 200 (item queries) + N (product lookups per item) = 400+ queries.

### Why the Fix Works at the Database and ORM Level

**`select_related("customer")`** tells Django's queryset compiler to perform a SQL `JOIN` between the `section01_order` and `section01_customer` tables in the initial SELECT statement. Instead of issuing 201 queries (1 for orders + 200 for customers), it issues 1 query with a LEFT OUTER JOIN. The database engine uses the existing foreign key index on `customer_id` to execute the join efficiently.

**`prefetch_related("items__product")`** tells Django to execute two separate queries — one for all `OrderItem` objects matching the orders in the result set, and one for all `Product` objects referenced by those items. Django then maps them in Python using a dictionary lookup (O(1) per access) instead of issuing a query per order. The `Prefetch` object with `select_related("product")` ensures that each item's product is joined in the item query, not fetched separately.

**`annotate(item_count=Count("items"))`** adds a SQL subquery `COUNT(section01_orderitem.id)` to the initial order SELECT, so `item_count` is computed at the database level rather than requiring a separate query or Python-level counting.

The result: 3 queries total regardless of result set size, down from 400+.

---

## Section 02 — SIGKILL Answer

### What happens to in-flight tasks if the Celery worker process is SIGKILL'd?

When a Celery worker receives SIGKILL (or crashes ungracefully), the following occurs:

1. **Unacknowledged tasks return to the queue.** With `CELERY_TASK_ACKS_LATE = True` (configured in our settings), the task is acknowledged *after* execution, not when the worker picks it up. If the worker dies mid-execution, the task message remains unacknowledged in the Redis broker. When another worker starts (or the same worker restarts), Redis redelivers the message.

2. **`reject_on_worker_lost = True`** ensures that if the worker process is killed, the task is explicitly rejected and requeued rather than being silently lost. This setting interacts with the `acks_late` behavior to provide double protection.

3. **The task's retry count resets.** Because the worker died before calling `self.retry()`, the `retries` counter on the request object is not incremented. The task is treated as a fresh attempt by the next worker that picks it up.

4. **Redis sorted set state is preserved.** The rate limiter's sorted set entries persist in Redis independently of the worker. If a task was rate-limited and waiting to retry, that state is lost (the retry countdown is in-memory), but the rate limit counter is unaffected.

5. **Potential duplicate execution.** If the worker completed the email send but died before acknowledging, the task will be retried and the email may be sent twice. The `_simulate_send_email` function (and real email providers) should be idempotent — using the `job_id` to deduplicate at the provider level.

In our implementation, the combination of `ack_late=True` and `reject_on_worker_lost=True` means no jobs are permanently lost on SIGKILL. The worst case is a delayed retry and potential duplicate send, which is handled by idempotent job IDs.

---

## Section 04 — Question A: Django Admin Performance

### Three root causes and fixes for slow Django admin with 500K+ records:

**1. N+1 queries in changelist view.** The default `ModelAdmin` renders each row by accessing related objects (e.g., `order.customer.name`). With 500K records displayed at 100 per page, this triggers 100+ queries per page load.

**Fix:** Add `list_select_related` to the `ModelAdmin` class. This tells Django's `ChangeList` to use `queryset.select_related()` for the listed fields:

```python
class OrderAdmin(admin.ModelAdmin):
    list_display = ["id", "customer_name", "status", "total_amount"]
    list_select_related = ["customer"]
```

This collapses the N+1 into a single JOIN query. For deeper nesting, use `list_select_related = ["customer", "items__product"]`.

**2. Missing `list_display` optimization — admin loads all fields.** By default, Django admin loads every column defined in `list_display`. If `list_display` includes computed methods or reverse relations, each row triggers additional queries.

**Fix:** Use `get_queryset()` to annotate computed fields at the database level instead of computing them per-row. For example, instead of a `order_count` method on `CustomerAdmin`, use:

```python
def get_queryset(self, request):
    return super().get_queryset(request).annotate(
        computed_order_count=Count("orders")
    )
```

Also add `raw_id_fields` for ForeignKey lookups in the changelist filter sidebar — this prevents Django from loading all related objects into `<select>` dropdowns:

```python
raw_id_fields = ["customer"]
```

**3. Admin page loads the full change form with all fields.** When a user opens a record with many fields or reverse FK inlines, Django admin issues queries for every inline and field. With 500K records, even the pagination query can be slow if there's no covering index.

**Fix:** Add `list_per_page` to reduce page size, and use `list_filter` with indexed fields to reduce the working set. For the change form specifically, use `fieldsets` to organize fields and `filter_horizontal` or `filter_vertical` for M2M fields. For very large tables, add `show_full_result_count = False` to skip the expensive `COUNT(*)` query that Django runs to show "Showing X of Y results":

```python
class LargeOrderAdmin(admin.ModelAdmin):
    show_full_result_count = False  # Skips the COUNT query
    list_per_page = 25
    list_filter = ["status", "created_at"]  # Filter on indexed columns
```

---

## Section 04 — Question C: File Upload Security

### Five attack vectors and Django-layer mitigations:

**1. Malicious file content disguised with a valid extension (polyglot files).**
A user uploads a file named `image.jpg` that is actually a PHP script or HTML with embedded JavaScript. The extension is valid but the content is malicious.

**Mitigation:** Validate file content using Django's `FILE_UPLOAD_HANDLERS` and a custom validator that inspects the file's MIME type via `python-magic` (not just the extension). Use `content_type` validation in the serializer:

```python
import magic
def validate_file_content(file):
    mime = magic.Magic(mime=True)
    file_type = mime.from_buffer(file.read(1024))
    file.seek(0)
    ALLOWED_TYPES = ["image/png", "image/jpeg", "application/pdf"]
    if file_type not in ALLOWED_TYPES:
        raise ValidationError(f"File type {file_type} not allowed")
```

**2. Path traversal in uploaded filenames.**
A user uploads a file with a name like `../../etc/passwd` or `..\\windows\\system32\\config`. Django's default `upload_to` uses `uuid` generation, but if custom `FileField` storage uses the original filename, the OS path can be manipulated.

**Mitigation:** Always use `upload_to` with a callable that generates safe filenames, and sanitize with `django.utils.text.get_valid_filename()`:

```python
from django.utils.text import get_valid_filename
def safe_upload_to(instance, filename):
    return os.path.join("uploads", get_valid_filename(filename))
```

Also set `FILE_UPLOAD_DIRECTORY_PERMISSIONS` and ensure the upload directory is outside the web root.

**3. Denial-of-service via extremely large file uploads.**
A user uploads a 10GB file to exhaust disk space, memory, or bandwidth. Django streams uploads to temp files by default, but large files can still cause OOM or disk exhaustion.

**Mitigation:** Set `DATA_UPLOAD_MAX_MEMORY_SIZE` and `FILE_UPLOAD_MAX_MEMORY_SIZE` in Django settings to limit in-memory and total upload size. For additional control, override `FileUploadHandler`:

```python
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5MB in memory
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10MB total
```

In the view/serializer, enforce a maximum file size before writing to storage.

**4. Cross-site scripting (XSS) via SVG or HTML file uploads.**
A user uploads an SVG file containing `<script>` tags or an HTML file. If served directly without proper Content-Type headers, the browser executes the embedded script.

**Mitigation:** Never serve uploaded files directly from Django in production. Use `DEFAULT_FILE_STORAGE` with a backend that sets correct Content-Type headers. Strip XML/SVG scripts using `defusedxml` before storage:

```python
import defusedxml.ElementTree as ET
def sanitize_svg(file):
    tree = ET.parse(file)
    for elem in tree.iter():
        if elem.tag.lower() == "script":
            elem.getparent().remove(elem)
    # Rewrite to sanitized file
```

Set `Content-Disposition: attachment` header when serving files to force download.

**5. Server-side request forgery (SSRF) via file upload URL imports.**
If the application accepts file uploads via URL (e.g., "upload from URL"), an attacker can point to internal services (e.g., `http://169.254.169.254/latest/meta-data/` on AWS) to access cloud metadata or internal APIs.

**Mitigation:** Validate and restrict the URL scheme, block private/internal IP ranges, and use Django's URL validation:

```python
from urllib.parse import urlparse
import ipaddress
def validate_upload_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("https",):
        raise ValidationError("Only HTTPS URLs allowed")
    # Resolve hostname and check against private ranges
    import socket
    ip = socket.gethostbyname(parsed.hostname)
    addr = ipaddress.ip_address(ip)
    if addr.is_private or addr.is_loopback:
        raise ValidationError("URL points to a private/internal address")
```

---

## Section 03 — Async Failure Modes of Thread-Local Tenant Scoping

### Why thread-locals fail in async Django

Python's `threading.local()` stores data per OS thread. In synchronous Django (WSGI), each request occupies one thread for its entire lifecycle, so thread-locals safely isolate request-scoped data.

In async Django (ASGI), a single thread can handle multiple concurrent requests via cooperative multitasking (`asyncio`). If request A sets `threading.local().tenant_id = 1` and request B (sharing the same thread) reads it before A cleans up, request B sees request A's tenant — a critical data leak.

### Why `contextvars` is the correct solution

`contextvars.ContextVar` (Python 3.7+) stores data per *execution context*, not per thread. When an `asyncio.Task` is created, it copies the current context, giving each task its own isolated view of the `ContextVar`. This means:

- Request A's `current_tenant_var` is isolated from Request B's, even on the same thread.
- Context propagates correctly through `await` chains within a single request.
- Cleanup is explicit — each request's middleware resets the `ContextVar` at the end.

### Additional async considerations

1. **Database connection isolation:** Each async view should use `database_sync_to_async` or Django's async ORM (Django 4.1+) to avoid sharing database connections across requests.

2. **Cache isolation:** Django's cache framework is synchronous by default. Use `django.core.cache.backends.locmem` carefully in async — it shares state across requests on the same thread. Prefer Redis-backed caches which are inherently request-independent.

3. **Celery task isolation:** Background tasks dispatched from async views should explicitly pass the `tenant_id` as a task argument, not rely on context propagation — Celery workers run in separate processes/threads.
