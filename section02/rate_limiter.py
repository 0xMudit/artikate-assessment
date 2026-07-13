"""
Sliding-window rate limiter using Redis sorted sets.

Architecture: Option B — Sliding Window with Redis Sorted Sets + ZREMRANGEBYSCORE

Why sliding window over the others:
- Token bucket (Option A): Requires maintaining a token count and refill rate.
  Simple but doesn't give precise per-minute control — bursts can briefly exceed
  the limit during refill cycles.
- Fixed window (Option C): Suffers from the "boundary problem" — 200 requests at
  11:59:59 + 200 at 12:00:01 = 400 requests in 1 second across windows.
- Sliding window (chosen): Tracks exact timestamps of each request. ZREMRANGEBYSCORE
  prunes old entries, ZCARD counts current window. No boundary problem, precise control.

Atomicity guarantee:
- All operations use a single Lua script evaluated via redis.eval().
  Lua scripts in Redis execute atomically — no other command interleaves.
  This is stronger than MULTI/EXEC (which allows interleaving between WATCH and EXEC)
  and pipelines (which batch but don't prevent race conditions).

Fail-open vs fail-closed:
- This implementation fails OPEN — if Redis is unreachable, the rate limiter
  allows the request through. Rationale: email delivery is time-sensitive
  (OTP, order confirmations). Blocking all emails because Redis is down is worse
  than occasionally exceeding the rate limit. The third-party provider has its own
  throttling and will return 429s which we can handle at the task level.
"""

import time

import redis


# Lua script for atomic sliding-window rate limiting.
#
# KEYS[1] = sorted set key for the window (e.g. "ratelimit:email-send")
# ARGV[1] = window size in seconds (e.g. 60)
# ARGV[2] = max requests per window (e.g. 200)
# ARGV[3] = current timestamp in microseconds (ensures unique sort scores)
#
# Returns: 1 if the request is allowed, 0 if it is rate-limited.
#
# Implementation notes:
# - ZREMRANGEBYSCORE prunes entries older than (now - window), making the set
#   a sliding window of the last `window` seconds.
# - ZCARD counts entries after pruning — O(1), fast.
# - ZADD adds the current request with a score equal to its timestamp in
#   microseconds. A unique sequence counter (via INCR on a companion key)
#   is used as the member value to prevent collisions at the same microsecond.
# - PEXPIRE sets the set's TTL so Redis garbage-collects it automatically.
RATE_LIMIT_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Remove entries outside the sliding window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window * 1000000)

-- Count current entries in window
local current = redis.call('ZCARD', key)

if current < limit then
    -- Use a Redis-incremented sequence number for uniqueness.
    -- math.random() can collide at the same microsecond, causing ZADD to skip.
    local seq = redis.call('INCR', key .. ':seq')
    redis.call('ZADD', key, now, now .. '-' .. seq)
    redis.call('PEXPIRE', key, window * 1000)
    return 1
else
    return 0
end
"""


class SlidingWindowRateLimiter:
    """
    Redis-backed sliding window rate limiter using an atomic Lua script.

    Each instance maintains a connection to Redis and a registered Lua script.
    The script executes atomically, so multiple workers can share the same
    Redis instance without race conditions.

    Usage example:
        limiter = SlidingWindowRateLimiter(redis_url="redis://localhost:6379/1")
        if limiter.allow("email-send", limit=200, window_seconds=60):
            send_email(...)
        else:
            # rate limited — retry after time_until_available()
            raise RetryError()

    Key structure in Redis:
        ratelimit:<key>      — sorted set, member = "<timestamp>-<seq>", score = timestamp
        ratelimit:<key>:seq  — integer counter for unique member IDs
    """

    def __init__(self, redis_url: str):
        """
        Initialize the rate limiter with a Redis connection.

        Registers the Lua script with Redis immediately so the SHA hash is
        cached. Subsequent calls use EVALSHA (faster than EVAL).

        Args:
            redis_url: Redis connection string (e.g. "redis://localhost:6379/1").
        """
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._script = self._redis.register_script(RATE_LIMIT_LUA)

    def allow(
        self,
        key: str,
        limit: int = 200,
        window_seconds: int = 60,
    ) -> bool:
        """
        Check whether a request is allowed under the rate limit.

        Executes the atomic Lua script: prune → count → add (if under limit).
        The entire operation is a single Redis round trip and executes atomically.

        If Redis is unreachable, this method returns True (fail-open). The
        ConnectionError from redis-py is caught and the request is allowed
        through. The rationale: email delivery is more important than strict
        rate limiting during Redis downtime.

        Args:
            key: Namespace key for the rate limit bucket.
                 Use fine-grained keys for per-user limiting (e.g. "email-send:user:123")
                 or coarse keys for global limiting (e.g. "email-send").
            limit: Maximum requests allowed in the window.
            window_seconds: Sliding window duration in seconds.

        Returns:
            True if the request is within the rate limit, False if exceeded.
        """
        now_microseconds = int(time.time() * 1_000_000)
        try:
            result = self._script(
                keys=[f"ratelimit:{key}"],
                args=[window_seconds, limit, now_microseconds],
            )
            return result == 1
        except redis.RedisError:
            # Fail-open: allow requests when Redis is unreachable
            return True

    def current_count(self, key: str, window_seconds: int = 60) -> int:
        """
        Return the number of requests recorded in the current sliding window.

        Prunes expired entries before counting so the result reflects the
        actual window state. This is non-atomic (two Redis commands) and is
        intended for monitoring only — do not use it for rate-limit decisions.

        Args:
            key: Namespace key (same as used in allow()).
            window_seconds: Window size to prune against.

        Returns:
            Integer count of requests in the current window.
        """
        now_microseconds = int(time.time() * 1_000_000)
        sorted_set_key = f"ratelimit:{key}"
        # Prune old entries first to get accurate count
        self._redis.zremrangebyscore(
            sorted_set_key, 0, now_microseconds - window_seconds * 1_000_000
        )
        return self._redis.zcard(sorted_set_key)

    def reset(self, key: str) -> None:
        """
        Clear the rate limit counter for a key.

        Deletes both the sorted set and the sequence counter. Used in tests
        to reset state between test cases.

        Args:
            key: Namespace key to reset.
        """
        self._redis.delete(f"ratelimit:{key}")

    def time_until_available(self, key: str, window_seconds: int = 60) -> float:
        """
        Return seconds until the oldest entry in the window expires.

        Used to set the retry countdown when a task is rate-limited — the
        task should retry after this duration (plus a small buffer) when the
        window will have room for a new request.

        If the sorted set is empty (no requests recorded), returns 0.0.

        Args:
            key: Namespace key to inspect.
            window_seconds: Window size to calculate expiry against.

        Returns:
            Float seconds until the oldest entry expires. 0.0 if no entries.
        """
        now_microseconds = int(time.time() * 1_000_000)
        sorted_set_key = f"ratelimit:{key}"
        oldest = self._redis.zrange(sorted_set_key, 0, 0, withscores=True)
        if not oldest:
            return 0.0
        oldest_ts = oldest[0][1]
        expires_at = oldest_ts + window_seconds * 1_000_000
        remaining = max(0, (expires_at - now_microseconds) / 1_000_000)
        return remaining
