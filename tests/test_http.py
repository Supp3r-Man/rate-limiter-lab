from ratelab import FakeClock, FixedWindow, TokenBucket
from ratelab.core import Decision
from ratelab.headers import rate_limit_headers
from ratelab.wsgi import RateLimitMiddleware


def hdrs(decision, limit=10, window=60.0):
    return dict(rate_limit_headers(decision, limit, window))


def test_headers_for_allowed_request_have_no_retry_after():
    h = hdrs(Decision(True, 7, 0.0, 12.2))
    assert h == {
        "RateLimit-Limit": "10",
        "RateLimit-Remaining": "7",
        "RateLimit-Reset": "13",
        "RateLimit-Policy": "10;w=60",
    }


def test_denied_retry_after_rounds_up_and_is_at_least_one():
    assert hdrs(Decision(False, 0, 2.01, 5.0))["Retry-After"] == "3"
    assert hdrs(Decision(False, 0, 0.0001, 5.0))["Retry-After"] == "1"
    assert hdrs(Decision(False, 0, 2.0, 5.0))["Retry-After"] == "2"


def test_denied_with_infinite_wait_omits_retry_after():
    assert "Retry-After" not in hdrs(Decision(False, 0, float("inf"), 0.0))


def test_zero_reset_and_negative_remaining_are_clamped():
    h = hdrs(Decision(True, -3, 0.0, 0.0))
    assert h["RateLimit-Remaining"] == "0" and h["RateLimit-Reset"] == "0"


def call(app, environ=None):
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"], captured["headers"] = status, dict(headers)

    body = b"".join(app(environ or {"REMOTE_ADDR": "203.0.113.9", "PATH_INFO": "/"}, start_response))
    return captured["status"], captured["headers"], body


def inner_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello"]


def test_wsgi_passes_through_with_ratelimit_headers():
    app = RateLimitMiddleware(inner_app, FixedWindow(2, 60.0, FakeClock()))
    status, headers, body = call(app)
    assert status == "200 OK" and body == b"hello"
    assert headers["RateLimit-Limit"] == "2" and headers["RateLimit-Remaining"] == "1"
    assert headers["Content-Type"] == "text/plain"


def test_wsgi_returns_429_with_retry_after_when_exceeded():
    clock = FakeClock()
    app = RateLimitMiddleware(inner_app, TokenBucket(1, 0.5, clock))  # 1 token, refill every 2 s
    assert call(app)[0] == "200 OK"
    status, headers, body = call(app)
    assert status == "429 Too Many Requests"
    assert headers["Retry-After"] == "2" and headers["RateLimit-Remaining"] == "0"
    assert b"rate limit exceeded" in body and headers["Content-Type"] == "application/json"
    clock.advance(2.0)
    assert call(app)[0] == "200 OK"


def test_wsgi_limits_per_client_key():
    app = RateLimitMiddleware(inner_app, FixedWindow(1, 60.0, FakeClock()))
    assert call(app, {"REMOTE_ADDR": "198.51.100.1"})[0] == "200 OK"
    assert call(app, {"REMOTE_ADDR": "198.51.100.1"})[0].startswith("429")
    assert call(app, {"REMOTE_ADDR": "198.51.100.2"})[0] == "200 OK"


def test_wsgi_denied_request_never_reaches_inner_app():
    calls = []

    def app(environ, start_response):
        calls.append(1)
        return inner_app(environ, start_response)

    mw = RateLimitMiddleware(app, FixedWindow(1, 60.0, FakeClock()))
    call(mw)
    call(mw)
    assert len(calls) == 1
