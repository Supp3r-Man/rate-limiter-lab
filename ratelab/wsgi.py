"""A tiny WSGI middleware example: 429 with headers when over the limit."""

from __future__ import annotations

from .headers import rate_limit_headers


def default_key(environ) -> str:
    return environ.get("REMOTE_ADDR", "unknown")


class RateLimitMiddleware:
    def __init__(self, app, limiter, key_func=default_key) -> None:
        self.app = app
        self.limiter = limiter
        self.key_func = key_func

    def __call__(self, environ, start_response):
        decision = self.limiter.allow(self.key_func(environ))
        extra = rate_limit_headers(decision, self.limiter.limit, self.limiter.window)
        if not decision.allowed:
            body = b'{"error": "rate limit exceeded"}\n'
            headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))] + extra
            start_response("429 Too Many Requests", headers)
            return [body]

        def wrapped(status, headers, exc_info=None):
            return start_response(status, list(headers) + extra, exc_info)

        return self.app(environ, wrapped)
