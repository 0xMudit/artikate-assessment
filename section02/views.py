"""
Section 02 — API views for the rate-limited async job queue.

Endpoints:
  POST /api/queue/send/         — queue a single email
  POST /api/queue/send-batch/   — queue a batch of emails
  GET  /api/queue/rate-status/  — check current rate limit usage
  POST /api/queue/reset-rate/   — reset rate limit counter (testing utility)
"""

import uuid

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .rate_limiter import SlidingWindowRateLimiter
from .tasks import send_batch_emails, send_transactional_email


class SendEmailSerializer(serializers.Serializer):
    """
    Validates the request body for a single email send.

    Fields:
        to:       Recipient address — validated as a proper email format.
        subject:  Subject line — max 200 chars to prevent oversized headers.
        body:     Email body — no length limit at the API layer.
        job_id:   Optional UUID for idempotency tracking. Auto-generated if omitted.
    """

    to = serializers.EmailField()
    subject = serializers.CharField(max_length=200)
    body = serializers.CharField()
    job_id = serializers.UUIDField(required=False, default=uuid.uuid4)


class SendBatchEmailSerializer(serializers.Serializer):
    """
    Validates the request body for a batch email send.

    Wraps a list of SendEmailSerializer objects so each recipient is
    individually validated before any tasks are dispatched.
    """

    recipients = SendEmailSerializer(many=True)


class SendEmailView(APIView):
    """
    Queue a single transactional email for delivery.

    Validates the request body, assigns a job_id if not provided, then
    dispatches a send_transactional_email Celery task. The task handles
    rate limiting, retry logic, and dead-letter handling asynchronously.

    Returns 202 Accepted immediately — the email is queued, not yet sent.
    The `task_id` can be used to check task status via Celery's result backend.

    Request body:
        {
            "to": "user@example.com",
            "subject": "Your order is confirmed",
            "body": "Hello...",
            "job_id": "optional-uuid"
        }

    Response:
        {
            "task_id": "<celery-task-id>",
            "job_id": "<uuid>",
            "status": "queued"
        }
    """

    def post(self, request):
        """Validate request, dispatch Celery task, return task ID."""
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        job_id = str(data.get("job_id", uuid.uuid4()))

        result = send_transactional_email.delay(
            to=data["to"],
            subject=data["subject"],
            body=data["body"],
            job_id=job_id,
        )

        return Response(
            {"task_id": result.id, "job_id": job_id, "status": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )


class SendBatchEmailView(APIView):
    """
    Queue a batch of transactional emails for delivery.

    Validates all recipients up-front, then dispatches a single
    send_batch_emails task which internally creates a Celery group.
    Each email in the group is processed independently with its own
    rate limiting and retry logic.

    Returns 202 Accepted with the Celery group ID. The batch is dispatched
    asynchronously — the response does not wait for delivery.

    Request body:
        {
            "recipients": [
                {"to": "a@example.com", "subject": "...", "body": "..."},
                ...
            ]
        }

    Response:
        {
            "group_id": "<celery-group-id>",
            "total": N,
            "status": "dispatched"
        }
    """

    def post(self, request):
        """Validate all recipients, assign job IDs, dispatch batch task."""
        serializer = SendBatchEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        recipients = serializer.validated_data["recipients"]
        # Assign job IDs for idempotency tracking if not provided by the caller
        for r in recipients:
            if "job_id" not in r:
                r["job_id"] = str(uuid.uuid4())

        result = send_batch_emails.delay(recipients=recipients)

        return Response(
            {
                "group_id": result.id,
                "total": len(recipients),
                "status": "dispatched",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class RateLimitStatusView(APIView):
    """
    Return the current state of the email send rate limit.

    Queries the SlidingWindowRateLimiter directly (non-atomically) to show
    how many requests have been made in the current 60-second window and
    how many slots remain.

    Useful for monitoring dashboards and for verifying rate limiter behaviour
    during tests without making actual send requests.

    Response:
        {
            "current_count": N,
            "limit": 200,
            "window_seconds": 60,
            "remaining": 200 - N
        }
    """

    def get(self, request):
        """Query current rate limit count and return remaining capacity."""
        limiter = SlidingWindowRateLimiter(settings.REDIS_URL)
        count = limiter.current_count("email-send", window_seconds=60)
        return Response(
            {
                "current_count": count,
                "limit": 200,
                "window_seconds": 60,
                "remaining": max(0, 200 - count),
            }
        )


class ResetRateLimitView(APIView):
    """
    Reset the email send rate limit counter.

    Intended for use in testing and development — clears the Redis sorted set
    used by the sliding window rate limiter so tests can start from a clean state.

    Not suitable for production use without authentication, as it allows bypassing
    the rate limit at will.

    Response:
        {"status": "reset"}
    """

    def post(self, request):
        """Delete the rate limit sorted set for the email-send key."""
        limiter = SlidingWindowRateLimiter(settings.REDIS_URL)
        limiter.reset("email-send")
        return Response({"status": "reset"})
