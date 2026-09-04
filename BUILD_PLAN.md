# Smart Market Watchlist — 45 Hour Build Plan
**CODE 2026 · Build window 4–7 Sep · Target completion: hour 45**

---

## 1. Product thesis

An attention filter, not a price tracker. It tells you what actually happened to your stocks since you last looked, explains why it mattered, and stays quiet about everything else.

Four claims to defend in Q&A:

1. **Raw percentage change is not meaning.** A move is scored against that stock's own normal volatility and against the market's move, so market-wide noise doesn't masquerade as stock-specific news.
2. **"What changed" is relative to *your* last visit,** not a fixed 24h window. A read watermark, like an unread inbox.
3. **A diff between two endpoints misses everything that reverted.** Events are recorded when they happen, so a spike-and-revert is still reported even though the price is unchanged.
4. **Restraint is a feature.** Capped digest, explicit suppressed count, and user-controlled mute.

---

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | React + Vite — Cloudflare Pages | Free, static, no cold start |
| API | FastAPI — Render free tier | Fast to write, pairs with the data layer |
| DB | Postgres — **Neon** | Free tier won't pause or expire before 18 Sep |
| Scheduler | Cloudflare Worker cron → Render ingest endpoint | Drives polling **and** keeps Render warm |
| Data | Provider interface: yfinance + replay feed | Demo cannot be broken by a flaky upstream |

**Two deployment landmines.** Render free spins down after ~15 min idle with a slow cold start — the cron trigger solves this as a side effect, which is a nice trade-off to explain. And yfinance is unofficial Yahoo scraping that gets rate-limited from datacenter IPs, so it may work locally and fail on Render.

**The interface is the deliverable, not the replay feed.** Framing to have ready: *"We isolated the data source behind an adapter so provider flakiness can't reach our correctness logic, and so the same logic is deterministically testable."* That reads as design. "We used fake data because the API broke" reads as a workaround — identical code, different perceived judgment.

---

## 3. Core architecture: detection is symbol-scoped, ranking is user-scoped

State this in exactly these terms. It resolves the tension between "scales to 100k users" and "what deserves **your** attention," and it's better than either pure approach.

**Detection (once per symbol).** Snapshots, baselines, scoring, event emission. Expensive statistical work, computed once regardless of how many people watch the symbol. If 50k people watch RELIANCE you compute once, not 50k times — O(unique symbols), not O(users × symbols).

**Ranking (per user, at read time).** Cheap personalization: their target price, their notes, their muted signal types, and how long they've been away. You're already joining events against watchlist items, so this costs nothing extra.

---

## 4. Schema

See `backend/schema.sql`.

---

## 5. Scoring

Per symbol, per ingest cycle:

```
z_move     = pct_change / ret_stddev_30d
vol_ratio  = volume / avg_volume_20d
rel_move   = pct_change - index_pct_change
breach     = crossed wk52_high | wk52_low | user target_price
```

Composite, weighted, normalized 0-100. Rank, cap the digest, show suppressed count.

**Reason string** — highest value per hour of any feature you'll build:

> `RELIANCE   +3.1σ move · 2.4x avg volume · breaking 52-week high`

**Deliberate simplification to defend:** plain subtraction for `rel_move` rather than a beta-adjusted residual. Estimating beta on 30 days of data is noisier than the correction it buys. Say exactly that if asked.

**Index fallback — decide now, not at hour 10.** NSE sector index coverage through yfinance is inconsistent. When a sector index is unavailable, fall back to NIFTY 50 as a market proxy and **label it honestly in the reason string** (`vs. NIFTY`, not `vs. sector`). Market-relative is a defensible simplification; a silently missing input is not.

**Hysteresis and clustering** — a correctness issue in an attention product, not polish. Require a score to exceed the threshold by a margin to open an event and fall below by a margin to close it, so a stock oscillating at the boundary doesn't flap. Group by `cluster_key` so one continuous 4% slide is a single evolving event with an updated `last_updated_ts`, not forty separate flags.

---

## 6. The hero: revert detection

A naive snapshot-vs-snapshot diff misses everything that happened and came back. If a stock spiked 5% on heavy volume and reverted while the user was away, the current price is identical to when they left — and a diff shows nothing.

The events table already records events **at the time they occur**, so the digest is a query over events since the watermark, not a comparison of two endpoints. Surface it explicitly:

> **The price is exactly where you left it. Here's what you missed.**
> Peaked +5.1% at 11:42 on 3.2x volume, reverted by 14:10.

Seed a spike-and-revert scenario into the replay feed so you can demonstrate this deterministically on camera. This single screen proves it isn't a price tracker better than any amount of explanation.

---

## 7. Time-scaled materiality

"Since you last checked" means something different over ten minutes than over a week. Let `gap = now - last_viewed_at`:

- **Short gap** — current delta is the story. Show live state plus events, normal threshold.
- **Long gap** — the endpoint is nearly meaningless; the **path** is the story. Show peak, trough, max drawdown, net move, and the count of distinct events. Raise the threshold so a week of ordinary drift doesn't generate fourteen flags.

Cheap to build (aggregates over rows you already have), follows directly from taking the brief literally, and almost nobody else will do it.

---

## 8. Seeded baselines

Your 30-day stddev, 20-day average volume and 52-week bounds all need historical data — which is exactly what a blocked provider can't give you. If yfinance fails from Render's IP you lose live quotes *and* the ability to compute baselines at all.

**Fetch history once, locally, and seed it into Neon.** Baselines then live in the database and refresh opportunistically. Never put a live historical fetch on the critical path. Record `history_days` on each baseline row so you can degrade gracefully for symbols with thin history rather than emitting garbage z-scores.

---

## 9. Data integrity (the rubric names these explicitly)

- **Idempotent ingest** on `(symbol, source_ts)` — a redelivered or duplicated fetch cannot double-count.
- **Monotonic writes** — reject a snapshot older than what you hold. Out-of-order arrival is normal with flaky providers.
- **Staleness visible, never smoothed** — show `as of 14:32 · delayed ~15min`. On fetch failure serve last-known-good behind a stale badge. Never blank, never a confident lie.
- **Source disagreement** — priority order plus a "sources differ" indicator. Do not average two sources into a price nobody reported.
- **Market session awareness** — "unchanged, market closed" is a different claim from "we don't know." Handle holidays and pre-open.
- **Watermark monotonicity** — two devices reading at once must only ever advance it: `last_viewed_at = GREATEST(last_viewed_at, $new)`.
- **Mute + escape hatch** — per symbol and per signal kind, plus a "show everything" view. An attention filter that can't be tuned eventually becomes noise, and the failure mode users actually fear is the false negative.

---

## 10. Hour by hour

| Hours | Work |
|---|---|
| 0-2 | Decisions, schema, repo, **hello-world deployed through the full pipeline** |
| 2-4 | Seed historical data locally into Neon; compute and store baselines |
| 4-9 | Provider interface, yfinance impl, replay feed (incl. spike-and-revert scenario), idempotent + monotonic ingest |
| 9-15 | Scoring, event emission, clustering + hysteresis, reason strings, index fallback |
| 15-19 | Auth, watchlist CRUD, watermark, mute; digest API with time-scaled materiality + path aggregates |
| 19-28 | Frontend: watchlist, digest, **revert hero**, explainability panel, stale badges, suppressed count, mute UI |
| 28-32 | Resilience: chaos toggles, provider failover, circuit breaker, degraded modes |
| 32-35 | Scale: seed ~10k watchlist items, add indexes, measure and record latencies |
| 35-38 | Tests on scoring and edge cases specifically |
| 38-42 | README, architecture diagram, DECISIONS.md, **demo script + one full rehearsal** |
| 42-45 | Buffer, submit |

**Deploy at hour one, not hour forty.** A broken deploy found late has killed more hackathon submissions than bad code.

**Edge cases for the 35-38 block:** empty watchlist · brand-new user with no prior visit · symbol with insufficient history for baselines · all providers down · market closed · duplicate add · watermark in the future (clock skew) · every event muted.

---

## 11. Explicitly cut — put this in the README

Websockets and real-time streaming (polling is defensible when upstream data is delayed anyway — say so). Options and crypto. Push notifications. Charting beyond a sparkline. Social features. Learned/adaptive scoring from user feedback — mute is the cheap 90%, a trained model is a rabbit hole.

**LLM news-cause summaries: cut, not deferred.** A judge at a SEBI-regulated broker will ask what happens when it hallucinates a cause for a price move and attributes that to Groww's brand. High risk, zero rubric value. Linking raw headlines without asserting causation is acceptable if you somehow have spare time.

Naming what you chose not to build reads as judgment. Half-built stretch features read as poor scoping.

---

## 12. Two documents to maintain from hour zero

**README.md** (graded — treat as a deliverable): what "meaningful change" means and why · architecture diagram and data flow · what you deliberately did not build and why · how it scales · setup instructions · limitations and edge cases handled.

**DECISIONS.md**: a running log of every trade-off and the alternative you rejected. Nearly free to maintain while the reasoning is fresh, and it's the raw material for both the 18 Sep Q&A and the 30 Sep Finale deep-dive.

---

## 13. Demo choreography (5 min demo · 5 min Q&A)

Run everything off the replay feed so it's deterministic.

1. Open on the **digest**, not the watchlist. Lead with a reason string.
2. Click into the **explainability panel** — makes your engineering visible without narration.
3. **The revert hero.** "Price is where you left it. Here's what you missed."
4. Flip the **chaos toggle** live: kill the provider, show the stale badge and graceful degradation instead of a crash.
5. Close on the **suppressed count** as evidence of restraint.

Rehearse once at hour 38-42. The 5-minute limit is unforgiving and the ordering above front-loads your strongest material in case you're cut short.

---

## 14. Risk register

| Risk | Mitigation |
|---|---|
| yfinance blocked from Render IP | Provider interface + replay feed; baselines pre-seeded |
| Render cold start kills live demo | Cloudflare cron keeps it warm; rehearse against the deployed URL |
| Sector index unavailable | NIFTY 50 fallback, honestly labelled |
| Thin history on a symbol | `history_days` column; degrade rather than emit garbage z-scores |
| Losing the reasoning before 18 Sep | DECISIONS.md from hour zero |

---

## Open item

**Check whether the platform allows updating a submission after submitting.** If it does, submit a thin working version around hour 20-24 to secure an evaluation slot, then keep building and resubmit until the deadline. If not, this 45-hour plan stands as written.
