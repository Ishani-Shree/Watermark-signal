# Watermark

**An attention filter for the market, not another price tracker.**

Watermark tells you what actually changed since *you* last looked, explains why
it mattered, and stays quiet about everything else.

- **App:** https://watermark-signal.pages.dev
- **API:** https://watermark-signal.onrender.com
- Trade-off log: [`DECISIONS.md`](DECISIONS.md) · Build plan: [`BUILD_PLAN.md`](BUILD_PLAN.md)

---

## 1. What "meaningful change" means here

A watchlist that sorts by `% change` is a ranking of volatility, not of
importance. Four claims shape what Watermark does instead.

### 1.1 Raw percentage change is not meaning

A 2% move is noise for one stock and a headline for another. Every move is
scored against **that stock's own normal behaviour**, and against the market,
on four axes:

| Signal | Measure | Weight |
|---|---|---|
| Volatility-relative move | `pct_change / 30-day stddev of daily returns` | 40 |
| Volume | `volume / 20-day average volume` | 30 |
| Index-relative move | `pct_change − index pct_change` | 20 |
| Level breach | crossed the 52-week high or low | 10 |

Each component saturates (z at 3σ, volume at 3×, index divergence at 3pp), so
one extreme reading can carry a finding without needing extremes everywhere.
The result is a 0–100 composite, and every surfaced event states its own
arithmetic:

```
RELIANCE.NS   z=+4.5 move | 3.2x avg volume | breaking 52-week high | vs NIFTY +5.1%
```

**A deliberate simplification:** index-relative move is plain subtraction, not
a beta-adjusted residual. Estimating beta on 30 days of data is noisier than
the correction it buys. Where a sector index is unavailable we fall back to
NIFTY 50 and **say so in the label** (`vs NIFTY`, never `vs sector`) — a
market-relative comparison is defensible, a silently substituted input is not.

### 1.2 "What changed" is relative to *your* last visit

Not a fixed 24-hour window. Each user has a **read watermark**, exactly like an
unread inbox: the digest is everything that happened after it, and reading the
digest advances it. Two devices reading at once can only ever move it forward
(`GREATEST(...)`), never backward.

### 1.3 A diff between two endpoints misses everything that reverted

This is the core of the design. If a stock spikes 5% on heavy volume and falls
back while you are away, the price you return to is the price you left — and a
before/after diff shows **nothing at all**.

Watermark records events **when they happen**, so the digest is a query over
events since your watermark, not a comparison of two snapshots:

> **The price is where you left it. Here's what you missed.**
> `peaked 1639.60` `now 1562.00` `-4.7% off peak` `1 event while away`

A move is reported as a *path* whenever it has round-tripped (currently ≥2% off
its peak) — **not** merely when the gap is long. That distinction is a
correctness matter, not presentation: an event's headline describes the moment
it fired ("breaking 52-week high"), and that sentence becomes false once the
price falls back. Reporting it as a bare event would state something untrue.

### 1.4 Restraint is a feature

The digest is capped at 3 items and reports what it held back:

> **1 of 12** watched stocks broke pattern. The other 11 stayed quiet — checked,
> scored, and not worth showing.

Restraint is only legible against the size of what was checked, which is why
the digest returns `flagged_count` / `watched_count` rather than only a
suppressed count. Users can mute any signal kind per symbol; the failure mode
people actually fear is the false negative, so muting is per-signal rather than
all-or-nothing.

---

## 2. Architecture

**Detection is symbol-scoped. Ranking is user-scoped.** This single split is
what lets the system be both personal and scalable.

```
        Cloudflare Worker (cron, 10 min)
                    │  keeps Render warm AND drives polling
                    ▼
┌───────────────┐        ┌──────────────────────────────────────────┐
│ React + Vite  │  REST  │            FastAPI (Render)              │
│ Cloudflare    │◄──────►│                                          │
│ Pages         │        │  /ingest ──► DETECTION (once per symbol) │
└───────────────┘        │              score → hysteresis →        │
                         │              cluster → events            │
                         │                                          │
                         │  /digest ──► RANKING (per user, at read) │
                         │              events ⋈ watchlist, mute,   │
                         │              time-scaled materiality     │
                         │                                          │
                         │  GuardedProvider (breaker + chaos)       │
                         │    ├── yfinance   └── replay feed        │
                         └──────────────────┬───────────────────────┘
                                            ▼
                              ┌──────────────────────────────┐
                              │      Postgres (Neon)         │
                              │ symbol-scoped: symbols,      │
                              │   snapshots, baselines,      │
                              │   events                     │
                              │ user-scoped: users,          │
                              │   watchlist_items, read_state│
                              └──────────────────────────────┘
```

**Detection** — snapshots, scoring, event emission. Expensive statistical work,
done **once per symbol** no matter how many people watch it. If 50,000 users
watch RELIANCE we compute once, not 50,000 times: cost is `O(unique symbols)`,
not `O(users × symbols)`.

**Ranking** — cheap personalisation at read time: the user's mute list, their
target price, and how long they have been away. This is a join against rows we
were already fetching, so it adds essentially nothing per request.

The schema enforces the split rather than merely documenting it: prices, scores
and events belong to the **symbol** and are never duplicated per user.

### Event lifecycle: hysteresis and clustering

A score must exceed **50** to open an event but only fall below **35** to keep
one alive, so a stock oscillating on the boundary does not flap. Readings within
30 minutes extend the existing event rather than creating a new one — one
continuous 4% slide is a single evolving event, not forty separate flags.

An event's headline is its **peak** reading, not its latest. A move that spiked
hard and is now easing off is still described by what made it significant.

---

## 3. How it scales

- **Detection cost tracks interesting symbols, not total symbols.** Symbols
  scoring below the close threshold return before touching the database at all.
- **Writes are batched** into one statement per ingest cycle. Against a remote
  database the round trip, not the insert, is what grows with watchlist size.
  Measured: the full 8-step replay scenario runs in **13.5s at 48 symbols —
  the same as at 11 symbols.**
- **The read path is a single indexed query** per digest
  (`events (symbol, first_seen_ts)`, `snapshots (symbol, source_ts DESC)`).
- **Ingestion is decoupled from readers.** Users never trigger a provider fetch;
  the cron does. Traffic spikes cost reads, not upstream calls.

**What we would do next, and deliberately have not:** move ingestion to a work
queue so symbols shard across workers; add Redis in front of the latest-snapshot
read; partition `snapshots` by time. None of these are load-bearing at hackathon
scale, and building them now would be speculation dressed as engineering.

---

## 4. Reliability and data integrity

The upstream (`yfinance`) is unofficial scraping. It broke once *during this
build*, which shaped the following.

- **Two timestamps, deliberately.** `source_ts` is when the *price* is from
  (the provider's market timestamp); `fetched_at` is when *we* polled. This is
  not bookkeeping: it is the difference between "the price has not moved" and
  "we have not looked". A stock that has not traded for an hour has an old
  `source_ts` and a fresh `fetched_at`, and that is healthy — so the stale
  badge keys on `fetched_at`, and a row reads *"quoted 65m ago · checked just
  now"*.
- **Idempotent ingest** on `(symbol, source_ts)`. Because `source_ts` is the
  market timestamp, re-fetching an unchanged quote yields the same key and is
  deduped. Measured: ingesting the same quotes three times gives 48 new, then
  **0 new / 48 deduped**, then 0 / 48. On conflict only `fetched_at` is
  refreshed — the price row is immutable.
- **Latest is derived, never assumed.** "Current price" is always
  `MAX(source_ts)`, so an out-of-order arrival cannot corrupt what downstream
  code reads as current.
- **Circuit breaker.** Three consecutive failures opens it for 60s; one
  half-open probe then decides. Without it, a dead upstream means 48 doomed
  calls *per cycle, every cycle*.
- **Degrade, don't fail over.** When the provider is down we write **nothing**
  and let the last real snapshot age with a visible `stale` badge. Falling back
  to a second source would hand the user a price that did not come from where
  they think it did. Nothing is invented to fill a gap.
- **Staleness is measured by age, not by what ingest recorded.** A quote written
  as `live` is still stale if nothing has arrived since.
- **Failure isolation is per-symbol; breaker-open aborts the cycle.** One sick
  ticker is normal; a dead provider is not.

Try it: the **Kill provider** control simulates an outage on demand.

### Edge cases handled

Empty watchlist · brand-new user with no watermark (bounded 24h lookback) ·
symbol with insufficient history (recorded as `history_days`, skipped rather
than emitting garbage z-scores) · provider down · duplicate watchlist add
(idempotent) · unknown symbol (rejected) · every signal muted · concurrent
reads from two devices · quotes arriving out of order.

---

## 5. What we deliberately did not build

Naming these is a scoping decision, not an omission.

- **Websockets / real-time streaming.** Polling is defensible when the upstream
  data is itself delayed. Streaming would add infrastructure without adding truth.
- **LLM-generated "why it moved" summaries.** Cut, not deferred. A hallucinated
  cause attached to a real price move, on a broker's surface, is a
  reputational and regulatory problem that no rubric line rewards. Linking raw
  headlines without asserting causation would be the safe version.
- **Learned/adaptive scoring from user feedback.** Mute is the cheap 90%; a
  trained relevance model is a rabbit hole with no time to validate it.
- **Options, crypto, push notifications, charting beyond a sparkline, social
  features.**

**Known gap, logged rather than hidden:** fetching the market timestamp from
yfinance requires `.info`, a heavier scrape than `fast_info`. We pay that cost
per symbol rather than fabricate a timestamp; the right fix at scale is a batch
quote endpoint, not a cheaper lie. A quote that arrives without a market
timestamp is stored as `unverified_ts` so the affected rows are visible.

---

## 6. Running it locally

**Prerequisites:** Python 3.10+ (Render is pinned to 3.11.9), Node 20+, and a
Postgres database (Neon's free tier is what we use).

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env             # then set DATABASE_URL and JWT_SECRET

# Apply the schema. With psql installed:
psql "$DATABASE_URL" -f schema.sql
# ...or without it:
python scripts/apply_schema.py

python scripts/seed_baselines.py # fetches 1y history, seeds baselines
uvicorn app.main:app --reload --port 8000
```

`seed_baselines.py` also writes `app/providers/replay_baseline.json`, the
resting price and volume for each symbol used by the replay feed.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env             # VITE_API_BASE=http://127.0.0.1:8000
npm run dev
```

### Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 50 tests, ~1s, no database needed
```

They deliberately cover judgement rather than arithmetic — that a quiet stock
stays quiet, that a market-wide move is not mistaken for stock-specific news,
that a missing input degrades instead of crashing, that the reason string never
overclaims, and that the circuit breaker's half-open probe behaves. Two are
load-bearing for correctness elsewhere:

- `source_ts` is **stable while a price is unchanged** — the precondition that
  makes `ON CONFLICT (symbol, source_ts)` a real dedup guarantee. If it drifted,
  ingestion would silently double-count and nothing else would notice.
- Replayed scenario timestamps **never land in the future** — a future
  `source_ts` is never "since you last looked", so the event would resurface on
  every refresh no matter how many times it was read.

### Seeing it work

The replay feed's scenario plays out over 65 simulated minutes. Rather than
wait, press **Run scenario** in the app: it steps the feed's *clock* through
each scripted point and runs a real ingest + detection pass at each one. Nothing
is injected — the detection layer sees the same quotes it would have seen live
and reaches its own conclusions. It then rewinds your watermark, so you arrive
as someone who has been away.

**Kill provider** simulates an upstream outage; **Reset** clears events and
price history.

---

## 7. Configuration

| Variable | Where | Purpose |
|---|---|---|
| `DATABASE_URL` | backend | Postgres connection string |
| `JWT_SECRET` | backend | Signing key for auth tokens |
| `PROVIDER` | backend | `replay` (scripted) or `yfinance` (live) |
| `CORS_ORIGINS` | backend | Comma-separated allowed origins (defaults cover the deployed app and local Vite ports) |
| `PYTHON_VERSION` | Render | Must be `3.11.9` |
| `VITE_API_BASE` | frontend | Backend base URL |

Demo controls are available only when `PROVIDER=replay` — you cannot
fast-forward a live market, and that gate is the enforcement.

---

## 8. Layout

```
backend/
  tests/                 50 tests, no database required
  app/
    main.py              routes; ingest + detection cycle
    detection.py         scoring, hysteresis, clustering   (symbol-scoped)
    ranking.py           digest, watermark, materiality    (user-scoped)
    provider_health.py   circuit breaker + chaos switch
    providers/           adapter boundary: base, guarded, yfinance, replay
  scripts/seed_baselines.py
  schema.sql
frontend/src/
  App.jsx, api.js, components/
cloudflare-worker/       cron: drives polling, keeps Render warm
```
