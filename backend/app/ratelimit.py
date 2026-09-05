"""
A small fixed-window rate limiter for the auth endpoints.

Deliberately in-process and dependency-free. The deployment runs a single
worker (`WEB_CONCURRENCY=1` on Render), so an in-memory counter is accurate
here; the honest caveat is that it stops being accurate the moment there is
more than one process, at which point this belongs in Redis or at the edge.
That is a real limit, written down rather than papered over.

What it buys: `/auth/login` is otherwise an unlimited password-guessing
oracle, and every guess costs a bcrypt hash -- so it is also a cheap way to
exhaust the CPU of a small instance.
"""

import time
from collections import deque

from fastapi import HTTPException, Request, status

WINDOW_SECONDS = 60
MAX_ATTEMPTS = 10
# Bound the bookkeeping itself: without a cap, spraying unique source IPs
# would turn the limiter into its own memory-exhaustion vector.
MAX_TRACKED_CLIENTS = 10_000

_hits: dict[str, deque[float]] = {}


def _client_key(request: Request) -> str:
    # Render sits behind a proxy, so the socket address is the load
    # balancer. X-Forwarded-For's FIRST entry is the original client.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(now: float) -> None:
    for key in [k for k, v in _hits.items() if not v or now - v[-1] > WINDOW_SECONDS]:
        _hits.pop(key, None)


def rate_limit_auth(request: Request) -> None:
    """FastAPI dependency. Raises 429 once a client exceeds the window."""
    now = time.monotonic()
    key = _client_key(request)

    if key not in _hits and len(_hits) >= MAX_TRACKED_CLIENTS:
        _prune(now)
        if len(_hits) >= MAX_TRACKED_CLIENTS:
            # Full even after pruning: fail closed on the auth path rather
            # than silently stop limiting under exactly the conditions a
            # limiter exists for.
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests, try again shortly",
            )

    window = _hits.setdefault(key, deque())
    while window and now - window[0] > WINDOW_SECONDS:
        window.popleft()

    if len(window) >= MAX_ATTEMPTS:
        retry_after = int(WINDOW_SECONDS - (now - window[0])) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    window.append(now)


def reset() -> None:
    """Test hook -- the limiter is module state and would otherwise leak
    between test cases."""
    _hits.clear()
