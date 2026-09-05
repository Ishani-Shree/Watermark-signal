"""
Ingest + detection cycle: fetch quotes, store them idempotently, and
run the symbol-scoped detection layer over the result.

Lives outside the routers because both the cron endpoint and the demo
scenario runner drive it, and it is the one piece of real domain logic in
the request path.
"""

from sqlalchemy import text

from .config import settings
from .detection import compute_score, upsert_event
from .provider_health import ProviderUnavailable, health
from .providers import get_provider

# Symbols that are themselves market/sector indices -- they get snapshots
# and baselines like anything else, but no event scoring of their own
# (nothing to compare them against).
INDEX_SYMBOLS = {"^NSEI"}


def run_ingest_cycle(conn, provider=None) -> dict:
    """One full ingest + detection pass. Extracted so the cron endpoint and
    the demo scenario runner drive the exact same code path -- the demo
    fast-forwards the feed clock, it does not inject events.

    `provider` is overridable so a demo can replay a scripted day while the
    deployment itself runs on live data. Without that, showing the scenario
    would mean redeploying with a different provider -- on stage.
    """
    provider = provider or get_provider()
    confidence = "replay" if provider.source_name == "replay" else "live"

    rows = conn.execute(
        text(
            """
            SELECT s.symbol, s.market_index_symbol, s.sector_index_symbol,
                   b.ret_stddev_30d, b.avg_volume_20d, b.wk52_high, b.wk52_low
            FROM symbols s
            LEFT JOIN baselines b ON b.symbol = s.symbol
            """
        )
    ).mappings().all()
    symbol_info = {row["symbol"]: dict(row) for row in rows}

    events_touched = 0
    quotes_by_symbol = {}
    quotes_out = []
    rows_to_write = []

    failed_symbols = []
    breaker_tripped = False

    # One upstream call for the whole universe. With a remote provider the
    # request count is what scales with watchlist size -- and what gets you
    # throttled -- not the parsing.
    try:
        fetched = provider.get_latest_batch(list(symbol_info))
    except ProviderUnavailable:
        fetched, breaker_tripped = {}, True
    except Exception:  # noqa: BLE001 - upstream is unofficial
        fetched = {}

    for symbol in symbol_info:
        quote = fetched.get(symbol)
        if quote is None:
            # Absent from the batch: this symbol failed, the rest carry on.
            if not breaker_tripped:
                failed_symbols.append(symbol)
            continue

        # A live quote missing price/volume/prev_close is routine with an
        # unofficial upstream. Reject it HERE, per symbol -- letting a None
        # through means the float()/int() coercion below raises mid-batch and
        # aborts the insert for every other symbol too, defeating the
        # isolation this loop exists to provide.
        if quote.price is None or quote.volume is None or quote.prev_close is None:
            failed_symbols.append(symbol)
            continue

        quotes_by_symbol[symbol] = quote
        rows_to_write.append(
            {
                "symbol": quote.symbol,
                "source_ts": quote.source_ts,
                "fetched_at": quote.fetched_at,
                "price": quote.price,
                "volume": quote.volume,
                "prev_close": quote.prev_close,
                "source": quote.source,
                # A quote whose provider gave no market timestamp can't be
                # deduped reliably; label it rather than pretend otherwise.
                "confidence": confidence if quote.has_market_ts else "unverified_ts",
            }
        )
        quotes_out.append(
            {"symbol": quote.symbol, "price": quote.price, "source_ts": quote.source_ts.isoformat()}
        )

    # One batched round trip rather than one per symbol. The DB is remote,
    # so round trips -- not the inserts themselves -- are the cost that
    # grows with watchlist size.
    #
    # On conflict we refresh `fetched_at` only. The price row is immutable
    # (that is the idempotency guarantee); re-seeing the same quote is not a
    # new observation of a new price, but it IS evidence the pipeline is
    # alive, and that belongs in fetched_at.
    # Written as ONE statement over unnested arrays rather than executemany,
    # because RETURNING is not available on executemany -- and the
    # per-row insert/update flag is what makes the dedup guarantee
    # observable instead of merely asserted.
    ingested = 0
    refreshed = 0
    conflicts = 0
    if rows_to_write:
        result = conn.execute(
            text(
                """
                INSERT INTO snapshots
                    (symbol, source_ts, fetched_at, price, volume, prev_close, source, confidence)
                SELECT * FROM unnest(
                    CAST(:symbols AS text[]),
                    CAST(:source_tss AS timestamptz[]),
                    CAST(:fetched_ats AS timestamptz[]),
                    CAST(:prices AS double precision[]),
                    CAST(:volumes AS bigint[]),
                    CAST(:prev_closes AS double precision[]),
                    CAST(:sources AS text[]),
                    CAST(:confidences AS text[])
                )
                ON CONFLICT (symbol, source_ts) DO UPDATE
                    SET fetched_at = EXCLUDED.fetched_at,
                        -- Same symbol, same market instant, DIFFERENT price:
                        -- the source is disagreeing with itself. Keep the
                        -- first value we were given -- overwriting would
                        -- rewrite history and make the earlier reading
                        -- unrecoverable -- but record the disagreement so it
                        -- is visible rather than silently resolved.
                        confidence = CASE
                            WHEN snapshots.price IS DISTINCT FROM EXCLUDED.price
                                THEN 'conflicting'
                            ELSE snapshots.confidence
                        END
                RETURNING (xmax = 0) AS inserted, confidence
                """
            ),
            {
                "symbols": [r["symbol"] for r in rows_to_write],
                "source_tss": [r["source_ts"] for r in rows_to_write],
                "fetched_ats": [r["fetched_at"] for r in rows_to_write],
                "prices": [float(r["price"]) for r in rows_to_write],
                "volumes": [int(r["volume"]) for r in rows_to_write],
                "prev_closes": [float(r["prev_close"]) for r in rows_to_write],
                "sources": [r["source"] for r in rows_to_write],
                "confidences": [r["confidence"] for r in rows_to_write],
            },
        )
        written = result.fetchall()
        ingested = sum(1 for row in written if row[0])
        refreshed = len(written) - ingested
        conflicts = sum(1 for row in written if row[1] == "conflicting")

    for symbol, quote in quotes_by_symbol.items():
        if symbol in INDEX_SYMBOLS:
            continue

        info = symbol_info[symbol]
        index_symbol = info["sector_index_symbol"] or info["market_index_symbol"]
        index_quote = quotes_by_symbol.get(index_symbol)

        index_pct_change = None
        index_label = None
        if index_quote and index_quote.prev_close:
            index_pct_change = (index_quote.price - index_quote.prev_close) / index_quote.prev_close
            # Honest labeling: only call it "sector" if it actually is one,
            # otherwise it's the NIFTY fallback -- see BUILD_PLAN.md section 5.
            index_label = "sector" if info["sector_index_symbol"] else "NIFTY"

        score = compute_score(
            symbol=symbol,
            price=quote.price,
            prev_close=quote.prev_close,
            volume=quote.volume,
            baseline=info,
            index_pct_change=index_pct_change,
            index_label=index_label,
        )
        cluster_key = upsert_event(conn, quote.source_ts, quote.price, score)
        if cluster_key:
            events_touched += 1

    return {
        "checked": len(symbol_info),
        "ingested": ingested,
        # Non-zero means dedup did its job: the same quote arrived again and
        # was not double-counted.
        "deduped": refreshed,
        # The source contradicting itself: same instant, different price.
        "conflicts": conflicts,
        "events_touched": events_touched,
        "failed": len(failed_symbols),
        "breaker_tripped": breaker_tripped,
        "provider_health": health.snapshot(),
        "quotes": quotes_out,
    }
