"""
Rate limiting implementation for host-based requests.

This module provides a HostRateLimiter that ensures requests to different hosts
are rate-limited according to a minimum interval.
"""

from typing import Dict, Callable


class HostRateLimiter:
    """A rate limiter that applies minimum intervals between requests to each host."""

    def __init__(self, min_interval_s: float, clock: Callable[[], float] = None, sleep: Callable[[float], None] = None):
        """
        Initialize the rate limiter.

        Args:
            min_interval_s: Minimum time (in seconds) between requests to the same host
            clock: A callable that returns current time in seconds (default: time.monotonic)
            sleep: A callable that takes seconds and sleeps (default: time.sleep)
        """
        self.min_interval_s = min_interval_s
        self.clock = clock or __import__('time').monotonic
        self.sleep = sleep or __import__('time').sleep
        self._last_requests: Dict[str, float] = {}

    def wait(self, host: str) -> None:
        """
        Wait if necessary to respect the rate limit for the given host.

        Args:
            host: The host to rate-limit requests for
        """
        now = self.clock()
        last_request = self._last_requests.get(host)

        # A host we have never seen proceeds immediately; a recent one sleeps
        # out the remainder of the interval. clock() is read exactly once per
        # wait() so an injected fake clock sees one tick per call.
        if last_request is not None:
            elapsed = now - last_request
            if elapsed < self.min_interval_s:
                remaining = self.min_interval_s - elapsed
                self.sleep(remaining)
                now = now + remaining

        self._last_requests[host] = now