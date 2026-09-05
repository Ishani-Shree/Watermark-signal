# Demo script — 5 minutes + 5 minutes Q&A

**Rehearse against the deployed URL, not localhost.** Render's free tier sleeps
after ~15 min idle; the cron keeps it warm, but open the app a few minutes
before you present so the first request is not a cold start.

**Setup, done before you share your screen:**
1. Open https://watermark-signal.pages.dev and log in.
2. Watchlist should hold ~6–8 stocks (enough that "1 of 8" means something).
3. Press **Reset**, then **Run scenario** (~15s). Confirm the digest shows the
   RELIANCE revert.
4. Press **Run scenario** once more right before you start, so the digest is
   freshly populated when you begin.

---

## The five minutes

### 0:00 — Open on the digest, not the watchlist (30s)

> "This is a market watchlist. But I'm not opening on a list of prices — I'm
> opening on what changed since I last looked."

Point at the headline: **"The price is where you left it. Here's what you
missed."**

> "The price is 1562. When I left, it was 1560. Basically unchanged. A normal
> watchlist would show me nothing happened."

### 0:30 — The revert (the hero) (60s)

Point at the chips: `peaked 1639.60` · `now 1562.00` · `-4.7% off peak`

> "But it peaked at 1639 — that's +5%, on 3.2x normal volume, and it broke its
> 52-week high. Then it came back. If you diff two snapshots, that entire event
> is invisible. We record events *when they happen*, so we can tell you about a
> move that already reversed."

**This is the single most important sentence in the demo.** Say it slowly.

### 1:30 — Why this one, and why not the others (75s)

Click the card to open the explainability panel.

> "Nothing here is a black box. The move was 4.5 standard deviations against
> *this stock's own* volatility — a 2% move is noise for one stock and a
> headline for another, so we never rank on raw percentage. Volume was 3.2x its
> 20-day average. And it diverged from NIFTY by 5%, so this is stock-specific,
> not the whole market moving."

Then point at the line underneath: **"1 of 8 watched stocks broke pattern."**

> "The other seven were checked, scored, and deliberately not shown. Restraint
> is the product. Anything that shows you everything is just a feed."

### 2:45 — Failure handling (60s)

Press **Kill provider**.

> "Market data providers go down. Watch what happens."

Point at the red **Feed down** pill and the banner.

> "We write nothing. We don't fall back to a second source, because that hands
> you a price that didn't come from where you think it did. The last real price
> stays, labelled with its age. We'd rather show you stale data you can *see*
> is stale than a number nobody reported."

Press **Restore provider**.

> "Three consecutive failures opens a circuit breaker for 60 seconds, so we're
> not hammering a provider we already know is down."

### 3:45 — Close on the thesis (45s)

> "Most watchlists optimise for showing you more. This one optimises for showing
> you less — and being right about what's left. The read watermark means
> 'what changed' is relative to *your* last visit, not a fixed 24-hour window.
> Read it once and it goes quiet."

Refresh to show the digest going quiet. Stop at 4:30 — leave buffer.

---

## Q&A — the questions you should expect

**"Why not just use percentage change?"**
Because it ranks volatility, not importance. A 2% move on HDFC Bank (0.87%
daily stddev) is 2.3σ; the same move on TCS (2.1%) is under 1σ. Ranking on raw
percentage puts the *less* meaningful move on top.

**"How does this scale?"**
Detection is symbol-scoped, ranking is user-scoped. If 50,000 people watch
RELIANCE we score it once, not 50,000 times — O(unique symbols), not
O(users × symbols). Writes are batched into one round trip per cycle, and
symbols scoring below threshold never touch the database. Measured: the full
replay scenario runs in the same wall-clock time at 48 symbols as at 11.

**"What happens when the data source lies or disagrees?"**
Two timestamps: `source_ts` is when the price is from, `fetched_at` is when we
polled. That distinction is why a stock that hasn't traded for an hour reads as
"unchanged, freshly confirmed" rather than "stale" — and it's what makes
`ON CONFLICT (symbol, source_ts)` a real dedup guarantee. Redelivering the same
quote writes zero rows.

**"Why no AI summarising why a stock moved?"**
Cut deliberately, not for time. A hallucinated cause attached to a real price
move, on a broker's surface, is a reputational and regulatory problem. Linking
raw headlines without asserting causation would be the safe version.

**"Isn't the replay feed just fake data?"**
The *interface* is the deliverable. The data source sits behind an adapter so
provider flakiness can't reach the correctness logic, and so the same logic is
deterministically testable. The demo fast-forwards the feed's *clock* — every
event you saw was produced by the real detection pipeline, not injected.

**"What would you do next?"**
Move ingestion to a work queue so symbols shard across workers; use a batch
quote endpoint so the market timestamp doesn't cost a scrape per symbol; per-user
thresholds learned from what they actually open. Not built because none of them
are load-bearing yet, and building them now would be speculation.

**If asked something you don't know:** say so, then say what you'd do to find
out. Every trade-off is written down in `DECISIONS.md` with the alternative that
was rejected and why.
