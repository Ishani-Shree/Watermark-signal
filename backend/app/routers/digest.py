"""The digest: what changed since you last looked, and how hard to filter."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import text

from ..auth import get_current_user_id
from ..db import engine
from ..ranking import SENSITIVITY, acknowledge, build_digest

router = APIRouter(tags=["digest"])


class SensitivityRequest(BaseModel):
    sensitivity: str

    @field_validator("sensitivity")
    @classmethod
    def known_level(cls, value: str) -> str:
        if value not in SENSITIVITY:
            raise ValueError(f"sensitivity must be one of {sorted(SENSITIVITY)}")
        return value


class DigestAckRequest(BaseModel):
    cursor: datetime | None = None


@router.get("/digest")
def digest(show_all: bool = False, user_id: int = Depends(get_current_user_id)):
    """User-facing read path (BUILD_PLAN.md sections 3 and 7): joins the
    detection layer's events against this user's watchlist, applies mute
    settings, and applies time-scaled materiality -- individual events for
    a short gap since last visit, an aggregated peak/trough/event-count
    path summary for a long one.

    Side-effect free. Marking the digest read is a separate POST to
    /digest/ack -- a GET that advanced the watermark consumed itself on any
    double fetch (a StrictMode double-invoke, a retry, a second tab), and
    the second, empty response was the one that reached the screen.
    """
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        return build_digest(conn, user_id, now, show_all=show_all)


@router.put("/settings/sensitivity")
def set_sensitivity(
    body: SensitivityRequest, user_id: int = Depends(get_current_user_id)
):
    """How hard the digest filters, saved per user.

    A filter the user cannot turn down is one they eventually stop trusting;
    this is the persistent version of the "show me everything" escape hatch.
    """
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET sensitivity = :s WHERE id = :uid"),
            {"s": body.sensitivity, "uid": user_id},
        )
    return {"sensitivity": body.sensitivity}


@router.post("/digest/ack")
def digest_ack(body: DigestAckRequest, user_id: int = Depends(get_current_user_id)):
    """Mark the digest read, up to the instant the client actually saw.

    Advancing to the client's cursor rather than to `now` means a signal
    that arrived between rendering and acknowledging is not skipped over.
    """
    now = datetime.now(timezone.utc)
    cursor = body.cursor or now
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    with engine.begin() as conn:
        acknowledge(conn, user_id, cursor, now)
    return {"ok": True}
