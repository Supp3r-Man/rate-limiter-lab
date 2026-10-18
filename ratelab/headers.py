"""RateLimit-* and Retry-After header values (following the IETF draft field names)."""

from __future__ import annotations

import math

from .core import Decision


def _ceil_seconds(x: float) -> int:
    return int(math.ceil(x - 1e-9))


def rate_limit_headers(decision: Decision, limit: int, window: float):
    """Return a list of (name, value) pairs.

    RateLimit-Limit      the quota
    RateLimit-Remaining  whole units left
    RateLimit-Reset      whole seconds until the state resets (rounded up)
    RateLimit-Policy     quota;w=window-seconds
    Retry-After          only when denied and the wait is finite; at least 1 second
    """
    headers = [
        ("RateLimit-Limit", str(int(limit))),
        ("RateLimit-Remaining", str(max(0, int(decision.remaining)))),
        ("RateLimit-Reset", str(_ceil_seconds(decision.reset_after) if decision.reset_after > 0 else 0)),
        ("RateLimit-Policy", f"{int(limit)};w={_ceil_seconds(window)}"),
    ]
    if not decision.allowed and math.isfinite(decision.retry_after):
        headers.append(("Retry-After", str(max(1, _ceil_seconds(decision.retry_after)))))
    return headers
