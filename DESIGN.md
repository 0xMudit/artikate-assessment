# DESIGN.md — Section 02: Rate-Limited Async Job Queue

## Architecture Decision

### Option Evaluation

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Celery + Redis** | Battle-tested, excellent retry/backoff support, Redis as both broker and rate-limit store, worker prefetching control, monitoring via Flower | Operational complexity (Redis + workers), memory overhead per worker | **Chosen** |
| **Django Q** | Simpler setup (no separate broker), uses Django ORM, built-in schedule | Limited retry semantics, no native rate limiting, ORM-based broker is slower under burst load, poor async support | Rejected |
| **Custom (Redis + asyncio)** | Full control, minimal dependencies | Must implement reliability guarantees from scratch (at-least-once delivery, dead-letter), no built-in monitoring, high implementation risk | Rejected |

### Why Celery + Redis

1. **Rate limiter + broker share the same Redis instance.** No additional infrastructure. The rate limiter's sorted sets and the task queue's lists both live in Redis, reducing operational surface.

2. **Celery's `ack_late=True` + `reject_on_worker_lost=True`** provides at-least-once delivery guarantee out of the box. If a worker crashes mid-task, the message returns to the queue. This is the exact behavior required ("does not lose jobs if the worker crashes mid-run").

3. **Native exponential backoff.** Celery's `self.retry(countdown=...)` with calculated delays handles the retry requirement without custom scheduling. The `max_retries` parameter prevents infinite retry loops.

4. **Redis rate limiter is atomic.** Using a Lua script executed via `redis.eval()`, the rate limiter's check-and-increment is atomic — no race conditions between workers. This is stronger than MULTI/EXEC pipelines.

## Rate Limiter Design

### Approach: Sliding Window with Redis Sorted Sets

**Why sliding window over token bucket and fixed window:**

- **Token bucket** allows brief bursts exceeding the rate during refill cycles. For an email provider that enforces strict 200/minute, this can cause 429 responses.
- **Fixed window** has the boundary problem: 200 requests at 11:59:59 + 200 at 12:00:01 = 400 in 1 second. Unacceptable for strict rate limiting.
- **Sliding window** tracks exact timestamps. `ZREMRANGEBYSCORE` prunes entries older than the window. `ZCARD` counts current entries. No boundary problem, precise per-minute control.

### Atomicity Guarantee

The rate limiter uses a **Lua script** evaluated via `redis.eval()`:

```lua
-- Atomic: prune old entries, count current, add if under limit
redis.call('ZREMRANGEBYSCORE', key, 0, now - window * 1000000)
local current = redis.call('ZCARD', key)
if current < limit then
    redis.call('ZADD', key, now, now .. '-' .. math.random(1000000))
    redis.call('PEXPIRE', key, window * 1000)
    return 1
else
    return 0
end
```

Lua scripts in Redis execute atomically — no other client command can interleave between lines. This is guaranteed by Redis's single-threaded command execution model.

### Fail-Open Strategy

If Redis is unreachable, the rate limiter **fails open** (allows the request). Rationale:

- Transactional emails (OTP, order confirmations) are time-sensitive. Blocking all emails because Redis is down is worse than occasionally exceeding the rate limit.
- The third-party email provider has its own rate limiting and will return HTTP 429, which the task's retry logic handles gracefully.
- In practice, Redis failure is rare and brief. The cost of a few extra emails during the outage is minimal compared to the cost of blocking critical notifications.

## Worker Configuration

```
CELERY_TASK_ACKS_LATE = True          # Ack after execution, not pickup
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # Requeue on worker crash
CELERY_TASK_SERIALIZER = "json"        # JSON for inspectability
```

`ack_late=True` is critical: it means the task message stays in the Redis list until the worker explicitly acknowledges it after successful completion. If the worker is SIGKILL'd mid-execution, the message is redelivered to another worker.

## Burst Handling

During a flash sale (2,000 requests in 10 seconds):

1. The API view dispatches 2,000 `send_transactional_email` tasks to Celery.
2. Celery's rate limiting (`rate_limit="200/m"`) throttles worker execution to 200/minute.
3. Tasks pile up in the Redis queue (which handles millions of messages efficiently).
4. The sliding window rate limiter provides a second layer: even if Celery's rate limit is bypassed (e.g., direct task invocation), the Redis limiter blocks excess sends.
5. Tasks exceeding the 5-retry max go to the dead-letter log with full context (to, subject, job_id, error).

Total processing time for 2,000 emails: ~10 minutes (200/minute × 10 batches). No jobs are lost — they queue safely in Redis.

## Dead-Letter Handling

Permanently failed tasks (after `max_retries=5`) are:

1. Logged with full context (recipient, subject, job_id, error message).
2. Return a `{"status": "dead_letter", ...}` dict with the failure details.
3. In production, this would be extended to write to a `DeadLetterJob` database table for manual inspection and reprocessing.

## What This Design Sacrifices

- **Immediate delivery under burst load:** Emails are delayed by the rate limit. This is an explicit trade-off — reliability and provider compliance over speed.
- **Exactly-once delivery:** The design guarantees at-least-once. Idempotent `job_id` at the provider level prevents duplicate side effects.
- **Redis as single point of failure:** Both the broker and rate limiter depend on Redis. In production, Redis Sentinel or Cluster would provide HA.
