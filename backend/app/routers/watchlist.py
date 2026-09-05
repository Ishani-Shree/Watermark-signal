"""Watchlist CRUD, and the market context shown alongside each holding."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from ..auth import get_current_user_id
from ..db import engine
from ..utils import _as_float

router = APIRouter(tags=["watchlist"])

MUTABLE_KINDS = {"z_move", "vol_spike", "relative_move", "level_breach", "target_hit"}


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=280)
    target_price: float | None = Field(default=None, gt=0, lt=1e9)


class MuteToggleRequest(BaseModel):
    """A single kind on or off, rather than a whole replacement array.

    `muted_kinds` on PATCH is a full replace, so two devices toggling
    different signals at the same time each send a list computed from the
    state they last saw -- and the second write silently undoes the first.
    A delta has no such window: the database applies it to whatever is
    actually there.
    """

    kind: str
    muted: bool

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in MUTABLE_KINDS:
            raise ValueError(f"unknown signal kind: {value}")
        return value


class WatchlistUpdateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=280)
    target_price: float | None = Field(default=None, gt=0, lt=1e9)
    muted_kinds: list[str] | None = Field(default=None, max_length=len(MUTABLE_KINDS))

    @field_validator("muted_kinds")
    @classmethod
    def known_kinds_only(cls, value: list[str] | None) -> list[str] | None:
        """Without this, `muted_kinds` is an arbitrary string array the client
        can write anything into -- unbounded junk that silently mutes nothing."""
        if value is None:
            return None
        unknown = set(value) - MUTABLE_KINDS
        if unknown:
            raise ValueError(f"unknown signal kinds: {sorted(unknown)}")
        return sorted(set(value))


@router.get("/symbols")
def list_symbols():
    """Populates the 'add to watchlist' picker on the frontend."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, name, exchange FROM symbols WHERE symbol != '^NSEI' ORDER BY name")
        ).mappings().all()
    return {"symbols": [dict(r) for r in rows]}


@router.get("/watchlist")
def get_watchlist(user_id: int = Depends(get_current_user_id)):
    """The watchlist view: current market state per holding.

    Returns the day's move, volume against its own 20-day norm, and where
    the price sits in its 52-week range -- not just a bare price. A price on
    its own is not market information: 1322 tells you nothing without
    knowing the stock closed at 1302 yesterday, is trading at 1.2x its usual
    volume, and sits 19% below its 52-week high.

    All of it comes from the snapshot and baseline rows already joined here,
    so it costs no extra round trip.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT w.symbol, s.name, w.note, w.target_price, w.muted_kinds, w.added_at,
                       latest.price, latest.source, latest.confidence,
                       latest.source_ts, latest.fetched_at,
                       latest.prev_close, latest.volume,
                       b.avg_volume_20d, b.wk52_high, b.wk52_low
                FROM watchlist_items w
                JOIN symbols s ON s.symbol = w.symbol
                LEFT JOIN baselines b ON b.symbol = w.symbol
                LEFT JOIN LATERAL (
                    SELECT price, source, confidence, source_ts, fetched_at,
                           prev_close, volume
                    FROM snapshots
                    WHERE snapshots.symbol = w.symbol
                    ORDER BY source_ts DESC
                    LIMIT 1
                ) latest ON true
                WHERE w.user_id = :uid
                ORDER BY w.added_at
                """
            ),
            {"uid": user_id},
        ).mappings().all()

    items = []
    for row in rows:
        item = dict(row)
        price = _as_float(row["price"])
        prev_close = _as_float(row["prev_close"])
        volume = row["volume"]
        avg_volume = _as_float(row["avg_volume_20d"])
        high = _as_float(row["wk52_high"])
        low = _as_float(row["wk52_low"])

        item["change_pct"] = (
            (price - prev_close) / prev_close * 100
            if price is not None and prev_close
            else None
        )
        item["volume_ratio"] = (
            volume / avg_volume if volume is not None and avg_volume else None
        )
        # Where in the 52-week band this price sits: 0 = at the low,
        # 1 = at the high. Context a raw number cannot give.
        item["range_position"] = (
            (price - low) / (high - low)
            if price is not None and high is not None and low is not None and high > low
            else None
        )
        items.append(item)

    return {"items": items}


@router.post("/watchlist")
def add_to_watchlist(body: WatchlistAddRequest, user_id: int = Depends(get_current_user_id)):
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM symbols WHERE symbol = :symbol"), {"symbol": body.symbol}
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Unknown symbol")

        # Idempotent add: UNIQUE (user_id, symbol) -- adding twice is a no-op,
        # not an error.
        conn.execute(
            text(
                """
                INSERT INTO watchlist_items (user_id, symbol, note, target_price)
                VALUES (:uid, :symbol, :note, :target_price)
                ON CONFLICT (user_id, symbol) DO UPDATE
                SET note = COALESCE(EXCLUDED.note, watchlist_items.note),
                    target_price = COALESCE(EXCLUDED.target_price, watchlist_items.target_price)
                """
            ),
            {
                "uid": user_id,
                "symbol": body.symbol,
                "note": body.note,
                "target_price": body.target_price,
            },
        )
    return {"ok": True}


@router.patch("/watchlist/{symbol}")
def update_watchlist_item(
    symbol: str, body: WatchlistUpdateRequest, user_id: int = Depends(get_current_user_id)
):
    fields = {}
    if body.note is not None:
        fields["note"] = body.note
    if body.target_price is not None:
        fields["target_price"] = body.target_price
    if body.muted_kinds is not None:
        fields["muted_kinds"] = body.muted_kinds

    if not fields:
        # Nothing to change is still a claim about a row that must exist.
        # Returning ok here would report success for a symbol the caller
        # does not have -- and for one nobody has.
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM watchlist_items WHERE user_id = :uid AND symbol = :symbol"
                ),
                {"uid": user_id, "symbol": symbol},
            ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Not in watchlist")
        return {"ok": True}

    # Column names are interpolated into SQL, so they must never come from
    # user input. They are literals above, but assert it: a later edit that
    # builds `fields` from a request body would otherwise turn this into an
    # injection point silently.
    assert set(fields) <= {"note", "target_price", "muted_kinds"}
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE watchlist_items SET {set_clause} WHERE user_id = :uid AND symbol = :symbol"
            ),
            {**fields, "uid": user_id, "symbol": symbol},
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"ok": True}


@router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, user_id: int = Depends(get_current_user_id)):
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM watchlist_items WHERE user_id = :uid AND symbol = :symbol"),
            {"uid": user_id, "symbol": symbol},
        )
    # Deleting nothing is not the same as deleting something. Reporting ok
    # for a symbol the caller never watched hides real client bugs -- the UI
    # would show the removal "working" while the row it meant to remove is
    # still there under a different user.
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"ok": True}


@router.post("/watchlist/{symbol}/mute")
def toggle_mute(
    symbol: str, body: MuteToggleRequest, user_id: int = Depends(get_current_user_id)
):
    """Mute or unmute one signal kind, as a delta.

    Computed by the database against the current row, so simultaneous
    toggles from two devices compose instead of overwriting each other --
    unlike PATCH muted_kinds, which replaces the whole array from whatever
    the client last saw.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE watchlist_items
                SET muted_kinds = CASE
                    WHEN :muted THEN
                        -- array_append only if absent, so repeat calls are
                        -- idempotent rather than accumulating duplicates.
                        CASE WHEN :kind = ANY(muted_kinds) THEN muted_kinds
                             ELSE array_append(muted_kinds, :kind) END
                    ELSE array_remove(muted_kinds, :kind)
                END
                WHERE user_id = :uid AND symbol = :symbol
                RETURNING muted_kinds
                """
            ),
            {"uid": user_id, "symbol": symbol, "kind": body.kind, "muted": body.muted},
        ).first()

    if result is None:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"symbol": symbol, "muted_kinds": sorted(result[0] or [])}
