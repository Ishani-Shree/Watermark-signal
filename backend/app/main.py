"""
Watermark API.

An attention filter for a market watchlist: what actually changed since you
last looked, and why it mattered.

This module only assembles the app. The work lives in:

  detection.py   scoring, hysteresis, clustering   (symbol-scoped)
  ranking.py     digest, watermark, materiality    (user-scoped)
  ingest.py      the ingest + detection cycle
  providers/     the data-source adapter boundary
  routers/       HTTP surface, one module per area
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import auth, demo, digest, meta, watchlist

app = FastAPI(
    title="Watermark API",
    description=(
        "What actually changed in your watchlist since you last looked. "
        "Moves are scored against each stock's own volatility, volume and "
        "52-week range -- not raw percentage change."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Cloudflare Pages gives every preview build its own subdomain, so those
    # cannot be enumerated ahead of time. The regex is anchored on both ends
    # to the exact project -- an unanchored pattern would also match a
    # hostile `watermark-signal.pages.dev.attacker.com`.
    allow_origin_regex=r"^https://[a-z0-9-]+\.watermark-signal\.pages\.dev$",
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # Auth travels as a Bearer header, never a cookie, so the browser never
    # needs to attach credentials cross-origin.
    allow_credentials=False,
)

app.include_router(meta.router)
app.include_router(auth.router)
app.include_router(watchlist.router)
app.include_router(digest.router)
app.include_router(demo.router)
