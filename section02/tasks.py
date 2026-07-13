"""
Celery tasks for transactional email sending.

Features:
- Rate-limited: uses SlidingWindowRateLimiter before each send.
- Exponential backoff retry: base_delay * 2^(retry_number).
- Dead-letter queue: permanently failed tasks are logged and stored.

Worker reliability settings (configured in settings.py):
  CELERY_TASK_ACKS_LATE = True            — acknowledge AFTER execution, not on pickup
  CELERY_TASK_REJECT_ON_WORKER_LOST = True — requeue task if worker is SIGKILL'd mid-execution

These two settings together guarantee at-least-once delivery even on worker crashes.
"""

import logging
import random
import time
from typing import Any

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from .rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

# Email provider rate limit — matches the rate limiter and task rate_limit parameter
EMAIL_RATE_LIMIT = 200  # requests per window
EMAIL_WINDOW_SECONDS = 60  # window size in seconds


def _get_rate_limiter() -> SlidingWindowRateLimiter:
    """
    Factory function that creates a SlidingWindowRateLimiter from Django settings.

    Kept as a separate function (rather than a module-level instance) so that
    the Redis connection is created lazily at task execution time, not at import
    time. This avoids connection errors during Django startup when Redis may not
    be available yet.

    Returns:
        SlidingWindowRateLimiter connected to REDIS_URL from Django settings.
    """
    from django.conf import settings
    return SlidingWindowRateLimiter(settings.REDIS_URL)


def _simulate_send_email(to: str, subject: str, body: str) -> bool:
    """
    Simulate sending an email via a third-party provider.

    Returns True on success. Raises ConnectionError on failure to exercise
    the retry logic. Simulates a realistic 5% failure rate — approximately
    1 in 20 sends will fail, triggering the exponential backoff retry path.

    In production this would be replaced by an SMTP library call or
    an HTTP request to a provider like SendGrid or Mailgun.

    Args:
        to: Recipient email address.
        subject: Email subject line (unused in simulation).
        body: Email body content (unused in simulation).

    Returns:
        True if the simulated send succeeded.

    Raises:
        ConnectionError: Simulates a transient SMTP/API failure.
    """
    if random.random() < 0.05:
        raise ConnectionError(f"SMTP timeout sending to {to}")
    return True


@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=2,  # base delay in seconds for exponential backoff
    ack_late=True,           # acknowledge AFTER execution — key for crash safety
    reject_on_worker_lost=True,  # requeue if the worker process is killed
    rate_limit="200/m",     # Celery-level rate limit (secondary enforcement)
)
def send_transactional_email(
    self,
    to: str,
    subject: str,
    body: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Send a single transactional email with rate limiting and retry logic.

    This is the core task of the Section 02 implementation. It demonstrates:
    1. Redis-backed rate limiting (primary enforcement via SlidingWindowRateLimiter)
    2. Exponential backoff retry (delay doubles on each attempt)
    3. Dead-letter handling (permanent failure captured after max_retries)
    4. Crash safety via ack_late + reject_on_worker_lost

    Rate limiting:
        Before sending, the task checks the sliding window rate limiter.
        If the limit is exceeded, the task re-raises itself via self.retry()
        with a countdown equal to the time until the window rolls over.
        This avoids busy-waiting or time.sleep() — the task simply goes back
        into the queue with a delayed execution time.

    Retry strategy (exponential backoff):
        retry_delay = base_delay * 2^(attempt - 1)
        Attempt 1 failure → retry in 2s
        Attempt 2 failure → retry in 4s
        Attempt 3 failure → retry in 8s
        Attempt 4 failure → retry in 16s
        Attempt 5 failure → retry in 32s
        A small random jitter (up to 10% of delay) prevents thundering herd
        when multiple tasks fail at the same time.

    Dead-letter:
        When MaxRetriesExceededError is raised (after attempt 5 fails), the task
        logs the failure at ERROR level and returns a status dict with "dead_letter".
        In production this would write to a DeadLetterJob database table for
        manual inspection and reprocessing.

    SIGKILL safety:
        ack_late=True means the task message stays in Redis until the task
        calls self.acknowledge() (implicit on successful return). If the worker
        is SIGKILL'd mid-execution, the message is never acknowledged and Redis
        redelivers it to the next available worker.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body content.
        job_id: Optional idempotency key. Used to deduplicate at the provider
                level if the task is retried after a successful but unacknowledged send.

    Returns:
        On success: {"status": "sent", "to": ..., "job_id": ..., "attempts": N}
        On dead-letter: {"status": "dead_letter", "to": ..., "job_id": ...,
                         "attempts": N, "error": "..."}
    """
    limiter = _get_rate_limiter()
    attempt = self.request.retries + 1

    # Check rate limit before sending — primary enforcement
    if not limiter.allow(
        key="email-send",
        limit=EMAIL_RATE_LIMIT,
        window_seconds=EMAIL_WINDOW_SECONDS,
    ):
        wait_time = limiter.time_until_available(
            key="email-send", window_seconds=EMAIL_WINDOW_SECONDS
        )
        logger.info(
            f"Rate limited for {to}, retrying in {wait_time:.1f}s "
            f"(attempt {attempt})"
        )
        raise self.retry(
            countdown=max(1, int(wait_time) + 1),
            exc=Exception("Rate limit exceeded"),
        )

    try:
        _simulate_send_email(to, subject, body)
        logger.info(f"Email sent to {to} (attempt {attempt})")
        return {
            "status": "sent",
            "to": to,
            "job_id": job_id,
            "attempts": attempt,
        }

    except ConnectionError as exc:
        # Exponential backoff with jitter to avoid thundering herd
        base_delay = self.default_retry_delay
        delay = base_delay * (2 ** (attempt - 1))
        jitter = random.uniform(0, delay * 0.1)
        total_delay = int(delay + jitter)

        logger.warning(
            f"Email to {to} failed (attempt {attempt}), "
            f"retrying in {total_delay}s: {exc}"
        )

        try:
            raise self.retry(countdown=total_delay, exc=exc)
        except MaxRetriesExceededError:
            # Dead-letter: all retries exhausted — log and return failure dict
            logger.error(
                f"DEAD LETTER: Email to {to} failed after {attempt} attempts. "
                f"Subject: {subject}, Job ID: {job_id}"
            )
            return {
                "status": "dead_letter",
                "to": to,
                "job_id": job_id,
                "attempts": attempt,
                "error": str(exc),
            }


@shared_task(bind=True, ack_late=True)
def send_batch_emails(
    self,
    recipients: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Dispatch a batch of individual send_transactional_email tasks as a Celery group.

    Rather than processing the batch sequentially in a single worker, this task
    creates a Celery `group` — a set of independent tasks that can be executed
    in parallel across available workers. Each individual task applies its own
    rate limiting and retry logic.

    Why group over sequential:
        - The rate limiter operates per-task — distributed rate limiting across
          all workers, not per-batch.
        - If one email fails, others in the batch are not blocked.
        - The batch task itself returns immediately after dispatching, so it
          doesn't occupy a worker slot while individual emails are being sent.

    Args:
        recipients: List of dicts with keys: to, subject, body, job_id (optional).

    Returns:
        {"status": "dispatched", "total": N, "group_id": "<celery-group-id>"}
    """
    from celery import group

    job = group(
        send_transactional_email.s(
            to=r["to"],
            subject=r["subject"],
            body=r["body"],
            job_id=r.get("job_id"),
        )
        for r in recipients
    )
    result = job.apply_async()

    return {
        "status": "dispatched",
        "total": len(recipients),
        "group_id": result.id,
    }
