from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .providers import get_provider

app = FastAPI(title="Watermark API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before submission
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.env, "provider": settings.provider}


@app.get("/ingest")
def ingest():
    """Cron target. Placeholder until the detection layer lands (hours 4-9)."""
    provider = get_provider()
    quote = provider.get_latest("RELIANCE.NS")
    return {"fetched": quote}


@app.get("/digest")
def digest():
    """User-facing read path. Placeholder until ranking layer lands (hours 15-19)."""
    return {"events": [], "suppressed_count": 0}
