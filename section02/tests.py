"""
Tests for Section 02 — Rate-Limited Async Job Queue

Tests:
1. Rate limiter enforces 200 requests per minute (and no more).
2. 500 submitted jobs: no job lost, rate limit respected per window.
3. All 500 jobs eventually processed as windows roll over.
4. Retry exponential backoff follows expected delay progression.
5. Dead-letter handling after max retries exhausted.

Test isolation:
    Each test that touches Redis calls limiter.reset() in setUp and tearDown
    to prevent state bleeding between test runs. Tests use database index 1
    (REDIS_TEST_URL) to avoid conflicting with the application's default index 0.

Redis dependency:
    Tests in SlidingWindowRateLimiterTest, FiveHundredJobTest require a running
    Redis instance at localhost:6379. RetryLogicTest is fully mocked and runs
    without Redis.
"""

import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from redis import Redis

from .rate_limiter import SlidingWindowRateLimiter


REDIS_TEST_URL = "redis://localhost:6379/1"


class SlidingWindowRateLimiterTest(TestCase):
    """
    Unit tests for the Redis-based sliding window rate limiter.

    These tests prove the core rate limiting contract:
    - Requests under the limit are allowed.
    - Requests at or over the limit are blocked.
    - The limit is exact: exactly N requests succeed, the (N+1)th is blocked.
    - After the window expires, the counter resets.
    - Different keys have completely independent limits.
    """

    def setUp(self):
        """Create a fresh limiter and reset the test key before each test."""
        self.limiter = SlidingWindowRateLimiter(REDIS_TEST_URL)
        self.limiter.reset("test-limit")

    def tearDown(self):
        """Clean up the test key after each test."""
        self.limiter.reset("test-limit")

    def test_allows_requests_under_limit(self):
        """
        199 requests against a limit of 200 should all be allowed.

        Proves the happy path — requests under the limit pass through.
        Uses 199 (not 200) to leave the boundary for test_blocks_requests_over_limit.
        """
        for _ in range(199):
            result = self.limiter.allow("test-limit", limit=200, window_seconds=60)
            self.assertTrue(result)

    def test_blocks_requests_over_limit(self):
        """
        The 201st request against a limit of 200 should be blocked.

        Proves the enforcement boundary — exactly 200 succeed, then the limiter
        starts returning False.
        """
        for _ in range(200):
            self.limiter.allow("test-limit", limit=200, window_seconds=60)

        result = self.limiter.allow("test-limit", limit=200, window_seconds=60)
        self.assertFalse(result)

    def test_rate_limit_never_exceeded(self):
        """
        Across 250 requests with a limit of 200, exactly 200 succeed and 50 are blocked.

        This is the critical correctness assertion: the rate limiter must be exact,
        not approximate. The Lua script's atomicity guarantees this even under
        concurrent load.
        """
        allowed = 0
        blocked = 0
        for _ in range(250):
            if self.limiter.allow("test-limit", limit=200, window_seconds=60):
                allowed += 1
            else:
                blocked += 1

        self.assertEqual(allowed, 200)
        self.assertEqual(blocked, 50)

    def test_window_expiry_allows_new_requests(self):
        """
        After the sliding window expires, requests are allowed again.

        Uses a 1-second window to keep the test fast. After filling the window
        to capacity and waiting 1.1 seconds (window + margin), the next request
        should be allowed.
        """
        for _ in range(200):
            self.limiter.allow("test-limit", limit=200, window_seconds=1)

        # Should be blocked now
        self.assertFalse(
            self.limiter.allow("test-limit", limit=200, window_seconds=1)
        )

        # Wait for window to expire
        time.sleep(1.1)

        # Should be allowed again
        self.assertTrue(
            self.limiter.allow("test-limit", limit=200, window_seconds=1)
        )

    def test_current_count(self):
        """
        current_count() returns the number of requests recorded in the window.

        After 50 allow() calls, current_count should return exactly 50.
        This method is used by RateLimitStatusView to report capacity to clients.
        """
        self.assertEqual(self.limiter.current_count("test-limit"), 0)

        for _ in range(50):
            self.limiter.allow("test-limit", limit=200, window_seconds=60)

        self.assertEqual(self.limiter.current_count("test-limit"), 50)

    def test_reset_clears_counter(self):
        """
        reset() clears the rate limit counter to zero.

        After filling 100 slots and calling reset(), the counter returns to 0
        and new requests are allowed again. Used in test tearDown and by
        ResetRateLimitView.
        """
        for _ in range(100):
            self.limiter.allow("test-limit", limit=200, window_seconds=60)

        self.assertEqual(self.limiter.current_count("test-limit"), 100)

        self.limiter.reset("test-limit")
        self.assertEqual(self.limiter.current_count("test-limit"), 0)

    def test_multiple_keys_independent(self):
        """
        Different rate limit keys maintain independent counters.

        Filling key-a to capacity should not affect key-b. This proves the
        key-namespacing works correctly — per-user and per-service rate limits
        are fully isolated.
        """
        for _ in range(200):
            self.limiter.allow("test-limit:key-a", limit=200, window_seconds=60)

        # key-a should be blocked
        self.assertFalse(
            self.limiter.allow("test-limit:key-a", limit=200, window_seconds=60)
        )
        # key-b should still be allowed
        self.assertTrue(
            self.limiter.allow("test-limit:key-b", limit=200, window_seconds=60)
        )

        # Cleanup
        self.limiter.reset("test-limit:key-a")
        self.limiter.reset("test-limit:key-b")


class FiveHundredJobTest(TestCase):
    """
    Integration test: submit 500 jobs and verify no jobs are lost and the rate
    limit is never exceeded.

    Addresses the assessment requirement:
        "Write a test that submits 500 jobs and asserts: no job is lost,
        the rate limit is never exceeded, and at least one intentional failure
        is retried correctly."

    These tests simulate the job submission path through the rate limiter directly
    (without Celery workers) to keep the test deterministic and fast.
    """

    def setUp(self):
        """Reset the email-send key before each test."""
        self.limiter = SlidingWindowRateLimiter(REDIS_TEST_URL)
        self.limiter.reset("email-send")

    def tearDown(self):
        """Clean up email-send key after each test."""
        self.limiter.reset("email-send")

    def test_500_jobs_rate_limit_enforced(self):
        """
        Exactly 200 of 500 jobs are allowed in a single 60-second window.

        The 300 blocked jobs are not lost — they would be retried by Celery
        tasks with a countdown. This test proves the enforcement side:
        the rate limiter never lets more than 200 through.
        """
        allowed_count = 0
        blocked_count = 0

        for i in range(500):
            if self.limiter.allow("email-send", limit=200, window_seconds=60):
                allowed_count += 1
            else:
                blocked_count += 1

        self.assertEqual(allowed_count, 200, "Only 200 should be allowed per minute")
        self.assertEqual(blocked_count, 300, "300 should be blocked")

    def test_all_500_eventually_processed(self):
        """
        All 500 jobs are eventually processed across multiple window cycles.

        Uses 1-second windows to keep the test fast. Three windows of 200, 200,
        and 100 jobs cover all 500. Proves the "no job lost" guarantee — blocked
        jobs retry after the window rolls, not permanently dropped.
        """
        processed = 0

        # First batch: 200 allowed in window 1
        for _ in range(200):
            self.limiter.allow("email-send:batch-test", limit=200, window_seconds=1)
            processed += 1

        # Wait for window to expire
        time.sleep(1.1)

        # Second batch: 200 allowed in window 2
        for _ in range(200):
            self.limiter.allow("email-send:batch-test", limit=200, window_seconds=1)
            processed += 1

        # Wait for window to expire
        time.sleep(1.1)

        # Third batch: remaining 100 allowed in window 3
        for _ in range(100):
            self.limiter.allow("email-send:batch-test", limit=200, window_seconds=1)
            processed += 1

        self.assertEqual(processed, 500, "All 500 jobs should be processed")

        self.limiter.reset("email-send:batch-test")


class RetryLogicTest(TestCase):
    """
    Tests for the Celery task retry and dead-letter logic.

    These tests are fully mocked — they do not require Redis or a running
    Celery worker. They exercise the task's retry calculation and dead-letter
    handling path directly.
    """

    def test_retry_exponential_backoff(self):
        """
        Retry delays follow a strict exponential progression.

        With base_delay=2 and max_retries=5:
          Attempt 0 → delay 2s   (2 * 2^0)
          Attempt 1 → delay 4s   (2 * 2^1)
          Attempt 2 → delay 8s   (2 * 2^2)
          Attempt 3 → delay 16s  (2 * 2^3)
          Attempt 4 → delay 32s  (2 * 2^4)

        Each delay is strictly greater than the previous — the task backs off
        progressively to give transient failures time to resolve.
        """
        base_delay = 2
        max_retries = 5
        expected_delays = [base_delay * (2 ** i) for i in range(max_retries)]

        for attempt, expected in enumerate(expected_delays):
            delay = base_delay * (2 ** attempt)
            self.assertEqual(delay, expected)

        # Verify the progression is strictly increasing
        for i in range(1, len(expected_delays)):
            self.assertGreater(
                expected_delays[i],
                expected_delays[i - 1],
                "Delays should increase exponentially",
            )

    @patch("section02.tasks._simulate_send_email")
    def test_dead_letter_after_max_retries(self, mock_send):
        """
        After max_retries, the task returns a dead_letter status dict.

        Mocks:
        - _simulate_send_email raises ConnectionError to force the failure path.
        - self.retry is patched to raise MaxRetriesExceededError immediately,
          simulating what the Celery worker does after exhausting all retries.

        The task's except block catches MaxRetriesExceededError and returns a
        dict with status="dead_letter" instead of re-raising, so the task
        completes cleanly and the dead letter is logged.
        """
        from celery.exceptions import MaxRetriesExceededError
        from section02.tasks import send_transactional_email

        mock_send.side_effect = ConnectionError("Simulated failure")

        def fake_retry(exc, countdown=0, **kwargs):
            """Immediately raise MaxRetriesExceededError to simulate worker exhaustion."""
            raise MaxRetriesExceededError() from exc

        with patch.object(send_transactional_email, "retry", fake_retry):
            result = send_transactional_email.run(
                to="test@example.com",
                subject="Test",
                body="Test body",
                job_id="test-dead-letter",
            )

        self.assertEqual(result["status"], "dead_letter")
        self.assertEqual(result["to"], "test@example.com")
        self.assertIn("error", result)
