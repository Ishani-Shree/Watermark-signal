"""
Demo controls.

Gated on their own switch rather than on which provider is configured --
tying them to the provider meant running on live data silently disabled the
demo, which is exactly when replaying a scripted day matters most.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from ..auth import get_current_user_id
from ..config import settings
from ..db import engine
from ..ingest import run_ingest_cycle
from ..provider_health import health
from ..providers import replay_provider as replay_clock
from ..providers.guarded import GuardedProvider
from ..providers.replay_provider import ReplayProvider

router = APIRouter(prefix="/demo", tags=["demo"])


def _require_demo_controls():
    """Demo controls are governed by their own switch, not by which provider
    happens to be configured.

    Tying them to `provider == replay` meant running on live data silently
    disabled the demo -- so the choice was "real data" OR "can demo it", and
    the only way to show the scenario was to redeploy with a different
    provider. On stage. They are also authenticated; this flag is the second
    lock, so an operator can turn them off entirely without a code change.
    """
    if not settings.demo_controls:
        raise HTTPException(status_code=403, detail="Demo controls are disabled")


def _replay_provider():
    """The scripted feed explicitly, whatever the deployment runs on. Still
    wrapped in the guard so a demo exercises the same breaker and chaos
    switch as production."""
    return GuardedProvider(ReplayProvider())


@router.post("/chaos")
def demo_chaos(enabled: bool, user_id: int = Depends(get_current_user_id)):
    """Flip the upstream provider into a simulated outage. Exists so the
    degraded path can be demonstrated deliberately (BUILD_PLAN.md section
    13, step 4) instead of hoping Yahoo misbehaves during the demo."""
    _require_demo_controls()
    health.chaos_enabled = enabled
    if not enabled:
        # Turning chaos off also clears the breaker, so recovery is
        # immediate on stage rather than after a 60s cooldown.
        health.record_success()
    return {"chaos_enabled": health.chaos_enabled, "provider_health": health.snapshot()}


@router.post("/run-scenario")
def demo_run_scenario(user_id: int = Depends(get_current_user_id)):
    """Step the replay clock through the whole scripted timeline, running a
    real ingest+detection pass at each point. Nothing is injected: the
    scoring layer sees the same quotes it would have seen live and reaches
    its own conclusions -- this only removes the wait. Makes the 5-minute
    demo deterministic (BUILD_PLAN.md section 13)."""
    _require_demo_controls()

    minutes = replay_clock.scenario_minutes()
    span = float(max(minutes)) if minutes else 0.0
    # Land the scenario in the recent past, ending now -- so it reads as
    # "what happened while you were away", not as future-dated quotes.
    anchor_end = datetime.now(timezone.utc)

    steps = []
    try:
        for minute in minutes:
            replay_clock.pin_minute(minute, anchor_end=anchor_end, span=span)
            with engine.begin() as conn:
                result = run_ingest_cycle(conn, provider=_replay_provider())
            steps.append(
                {
                    "minute": minute,
                    "ingested": result["ingested"],
                    "events_touched": result["events_touched"],
                }
            )
    finally:
        # Always hand the clock back to real time, even if a step raised.
        replay_clock.pin_minute(None)

    # The scenario represents time that passed while you were away, so put
    # the read watermark back to before it started -- otherwise the events
    # land behind a watermark that was advanced when the page loaded, and
    # the demo can never show its own scenario.
    #
    # This is the one place that deliberately moves a watermark BACKWARD.
    # The normal path is monotonic (see ranking._advance_watermark); this
    # is a demo-only rewind, gated to the replay feed.
    scenario_start = anchor_end - timedelta(minutes=span)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO read_state (user_id, last_viewed_at)
                VALUES (:uid, :ts)
                ON CONFLICT (user_id) DO UPDATE SET last_viewed_at = EXCLUDED.last_viewed_at
                """
            ),
            {"uid": user_id, "ts": scenario_start},
        )

    return {"steps": steps, "rewound_watermark_to": scenario_start.isoformat()}


@router.post("/reset")
def demo_reset(user_id: int = Depends(get_current_user_id)):
    """Clear all detected events and snapshots so a scenario can be re-run
    from a clean slate. Leaves users, watchlists and baselines intact.

    Requires a logged-in caller. Being gated to the replay feed is NOT a
    security control -- the deployed instance runs on the replay feed, so
    without authentication anyone holding the public URL could wipe the
    data this endpoint deletes.
    """
    _require_demo_controls()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM events"))
        conn.execute(text("DELETE FROM snapshots"))

    # A replayed scenario writes scripted prices over real ones (the script
    # is anchored to now, so it wins on source_ts). Clearing alone would
    # leave the app empty until the next cron tick -- up to ten minutes of
    # showing nothing. Re-ingesting immediately puts genuine prices back the
    # moment the demo ends.
    restored = {}
    try:
        with engine.begin() as conn:
            restored = run_ingest_cycle(conn)
    except Exception as exc:  # noqa: BLE001 - reset itself must still succeed
        return {"ok": True, "restored": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    return {
        "ok": True,
        "restored": True,
        "source": settings.provider,
        "symbols": restored.get("ingested", 0),
    }
