# Decisions log

Format: date/hour, decision, alternative rejected, why.

- **Hour 0-2** — Provider isolated behind an interface (`PriceProvider`) with
  `yfinance` and `replay` implementations. Rejected: calling yfinance directly
  from route handlers. Why: yfinance is unofficial Yahoo scraping and gets
  rate-limited from datacenter IPs (Render); the demo must not depend on it
  working live.
- **Hour 0-2** — Symbol-scoped vs user-scoped schema split from the start
  (`symbols/snapshots/baselines/events` vs `users/watchlist_items/read_state`).
  Rejected: a single denormalized table per user. Why: detection work must be
  computed once per symbol regardless of watcher count, not once per user.
- **Hour 2-4** — Bumped `yfinance` from 0.2.44 to 1.7.0. The pinned version
  returned empty/invalid responses for every ticker (Yahoo's undocumented API
  had moved on since that release). Confirms the plan's own risk register:
  this dependency is unofficial scraping and can break without warning —
  the provider-interface adapter exists precisely so this kind of breakage
  stays contained to one file.
- **Hour 2-4** — Dropped `TATAMOTORS.NS` from the seeded symbol list (404,
  not delisted-and-skipped but genuinely not found) in favor of
  `AXISBANK.NS`. Likely fallout from Tata Motors' 2025 demerger changing
  how that ticker resolves. Chose to swap rather than chase the renamed
  ticker — a live example of the "thin/missing history" edge case the
  plan calls out, handled by skipping and warning rather than crashing.
- **Hour 9-15** — Rescaled every replay-feed price/volume to sit realistically
  inside that symbol's REAL seeded baseline (wk52 range, avg_volume_20d),
  instead of arbitrary placeholder numbers. Caught via a direct unit test
  of `compute_score`, not by observation: the original synthetic prices
  (e.g. RELIANCE at ~2900 against a real wk52 range of [1258, 1585]) would
  have permanently flagged every symbol as breaching its 52-week range on
  every tick, drowning out the real signal and breaking the "digest stays
  quiet for boring stocks" story. The lesson generalizes: a replay/mock
  feed is only as good as its fidelity to the real baseline it gets
  compared against, not just to the real *shape* of a scenario.
- **Hour 9-15** — Reason strings use ASCII (`z=+4.5`, `|` separators)
  instead of `σ`/`·`. Caught the same way: a Windows console crashed
  (`UnicodeEncodeError`) printing the sigma character during local testing.
  Render's Linux runtime and the browser both handle UTF-8 fine either way,
  but there's no reason to leave a landmine for local debugging on Windows.
- **Hour 15-19** — Dropped `passlib` for password hashing; call `bcrypt`
  directly instead. `passlib` is unmaintained since 2020 and its bcrypt
  backend crashes outright against `bcrypt>=4.1` (a hardcoded internal
  self-test hits the library's own 72-byte input limit and raises instead
  of the version-detection path catching it). `bcrypt` itself works fine;
  only passlib's compatibility shim was broken. Direct calls are also just
  simpler -- one less abstraction layer for two functions.
- **Hour 45+ — the deployment runs on LIVE data.** The replay feed was
  chosen partly on an untested claim from our own plan: that yfinance would
  be rate-limited from a datacenter IP. `/diagnostics/live-provider` was
  added to answer that with evidence instead of assumption, and the answer
  is that it works. Live is now the production provider; replay is the
  fallback and the demo instrument, which is what the adapter was always for.
- **Hour 45+** — Live quotes are fetched in ONE batched request for the
  whole universe (48 symbols in ~3.5s) rather than per symbol via `.info`
  (~2.8s EACH, ~2 minutes for the universe). With a remote upstream the
  request count is what scales -- and what gets you throttled.
- **Hour 45+** — Volume and previous close come from DAILY bars; price and
  timestamp come from MINUTE bars. Mixing resolutions is deliberate and the
  alternative is a silent, catastrophic bug: `avg_volume_20d` is an average
  of *daily* volume, and RELIANCE's last minute-bar is ~635k against a 10.5M
  daily average -- so scoring a minute-bar volume against it would peg every
  stock at ~0.06x forever. The volume signal would go dead without ever
  raising an error. Daily timestamps alone were no good either: they change
  once a day, collapsing every poll into one snapshot and erasing the
  intraday path revert detection depends on.
- **Hour 45+** — Demo controls are gated on their own `DEMO_CONTROLS` flag,
  not on `provider == replay`. The old gate meant running on real data
  silently disabled the demo: the choice was "live data" OR "can demonstrate
  it", and showing the scenario would have meant redeploying with a
  different provider mid-presentation. `run_ingest_cycle` now takes a
  provider override so a scripted day can be replayed through the real
  pipeline while the deployment itself stays live.
- **Hour 45+** — `/demo/reset` re-ingests immediately instead of only
  clearing. A replayed scenario writes scripted prices over real ones (the
  script is anchored to now, so it wins on `source_ts`); clearing alone
  would leave the app empty until the next cron tick, up to ten minutes of
  showing nothing after a demo. Verified: RELIANCE goes from the scripted
  1562 back to the real 1322 the moment reset runs.
- **Hour 45+** — Providers declare their own `source_name`, and stored rows
  are labelled from the provider that actually ran rather than from the
  configured one. With an overridable provider, reading config would
  mislabel every demo row as live.
- **Hour 42-45 (audit)** — `PATCH`/`DELETE` on a watchlist item the caller
  does not have returned 200. Deleting nothing is not the same as deleting
  something: the UI would animate a removal that never happened, and a real
  client bug would be invisible. Both now 404.
- **Hour 42-45 (audit)** — `POST /demo/reset` and `POST /demo/chaos` were
  reachable **unauthenticated**. Being gated to the replay provider is not a
  security control: the deployed instance *runs* on the replay feed, so the
  gate was open. Anyone with the public URL could `DELETE FROM events` and
  `DELETE FROM snapshots`, then pin the provider into a permanent simulated
  outage so the cron never refilled it. Both now require a logged-in caller.
  The lesson: a flag that describes an environment is not a permission check.
- **Hour 42-45 (audit)** — `GET /digest` used to advance the read watermark,
  which made *reading consume itself*: React StrictMode double-invokes mount
  effects, so the first call marked everything read and the second returned
  empty -- and the empty one is what reached the screen. Any retry or second
  tab did the same. Reading is now side-effect free and the client sends an
  explicit `POST /digest/ack` carrying the cursor the digest reported, so a
  signal arriving between render and ack is not skipped. A GET that mutates
  is not merely impure here; it silently destroys the product's core screen.
- **Hour 42-45 (audit)** — A watermark in the future (clock skew) was
  unrecoverable: `lookback_start` became a future instant so nothing matched,
  and the `GREATEST(...)` advance meant the bad value could never be written
  back down. The user would see "still quiet" until wall-clock caught up.
  Now clamped -- a future watermark is treated as no watermark. This is the
  exact edge case BUILD_PLAN.md section 10 names, and it was unhandled.
- **Hour 42-45 (audit)** — Muted stocks were counted as ones that "stayed
  quiet". They were not quiet; the user silenced them. `muted_count` is now
  reported separately and the copy says so. In a product whose entire pitch
  is honest filtering, misreporting its own filtering is the worst class of
  bug it can have. The same error applied to symbols dropped by the long-gap
  threshold, which are now counted before the trim, not after.
- **Hour 42-45 (audit)** — Revert detection was peak-only, so a stock that
  crashed and recovered was never flagged and kept a "breaking 52-week low"
  headline after the price came back. Naive symmetry does not work: a rally
  still running also sits far above its trough. It needs the move's
  direction, which was not stored -- so `events.direction` is recorded at
  open, when the sign is unambiguous, and an up move is judged against its
  peak, a down move against its trough.
- **Hour 42-45 (audit)** — Input validation: email format, password 8-72
  bytes (bcrypt *raises* past 72, so an over-long password was a 500 rather
  than a 422), bounded note/target, and `muted_kinds` restricted to known
  kinds instead of an arbitrary string array the client could write anything
  into.
- **Hour 42-45 (audit)** — Rate limiting on `/auth/*`: 10 requests per
  minute per IP, in-process. Without it, `/auth/login` is an unlimited
  password oracle where every guess costs a bcrypt hash -- a brute-force
  vector and a cheap way to exhaust a small instance's CPU. The honest
  limitation, written in the module: an in-process counter is only accurate
  because the deployment runs a single worker; past that it belongs in Redis
  or at the edge.
- **Hour 42-45 (audit)** — Startup refuses to run in production with the
  development JWT secret. Verified the live deployment was already
  configured correctly (a token forged with the default was rejected), but
  the default is published in this repo, so a future deploy that forgot the
  env var would have silently accepted forged tokens for any account. It
  now fails loudly instead.
- **Hour 42-45** — Finished `target_price`, which had been stored, read and
  passed around while doing nothing. A half-built feature reads as poor
  scoping (BUILD_PLAN.md section 11), so the choice was wire it up or delete
  it; the plan's own scoring spec names it, so it was wired up. It also
  demonstrates the architecture rather than merely asserting it: a target is
  different for every user watching the same stock, so it CANNOT live in the
  symbol-scoped detection layer and is evaluated at read time. It costs a
  round trip only for users who set one.
- **Hour 42-45** — Target crossings are judged against the window's
  EXTREMES, not its endpoints. The first implementation compared the price
  at the watermark to the price now -- and missed RELIANCE crossing 1600 on
  its way to 1639 and back to 1562, because both endpoints sit below the
  target. That is precisely the blindness this product exists to fix, and I
  had reintroduced it inside a new feature. Caught by testing the feature
  against the demo scenario rather than a hand-picked case.
- **Hour 42-45** — A crossing requires *crossing*, not "price is past the
  target". A stock that has sat above your target for a week is not news
  every time you open the app; it was news the day it got there. When it
  crossed and came back the copy says "since pulled back", because "hit your
  target" beside a price nowhere near it reads as a bug.
- **Hour 32-35** — Load-tested the read path at 10,000 watchlist items
  (500 users x 20 symbols) and found the bottleneck was not where the
  README had assumed. The digest's own SQL executes in **0.13 ms**; a
  single round trip to Neon costs **~400 ms**. The endpoint was making
  five. Collapsed to two -- watermark and watchlist via LEFT JOINs in one,
  and events + current price + the watermark advance in another (the
  advance rides as a data-modifying CTE, since a write needing no result
  should not cost its own trip). Median digest latency 2,173 ms -> 1,333 ms.
  The lesson worth keeping: profiling said "round trips", and any time
  spent tuning the queries themselves would have bought nothing.
- **Hour 32-35** — Current price is now joined per *event* rather than
  fetched for the whole watchlist. Only symbols with events ever need it,
  so a 40-symbol watchlist with one event was fetching 39 prices to throw
  away.
- **Hour 32-35** — Added `idx_events_symbol_updated (symbol,
  last_updated_ts)`. The existing index was on `first_seen_ts`, which the
  digest never filters on -- it filters on `last_updated_ts`, because an
  event still being extended must keep surfacing. Postgres correctly
  seq-scans at current table size, which is exactly why this would have
  gone unnoticed until the table was large enough to hurt.
- **Hour 38-42** — CORS narrowed from `allow_origins=["*"]` to an explicit
  list plus an anchored regex for Cloudflare Pages preview subdomains
  (which get random names and cannot be enumerated ahead of time). The
  regex is anchored at both ends deliberately: an unanchored pattern would
  also match `watermark-signal.pages.dev.attacker.com`, which is a
  different domain entirely. Methods and headers are enumerated rather than
  `*`, and `allow_credentials` stays False because auth travels as a Bearer
  header and never as a cookie. Verified by preflighting each case,
  including the lookalike.
- **Hour 35-38** — Tests target judgement, not arithmetic. Asserting that
  `40 * min(z/3, 1)` equals what the same expression computes proves
  nothing; asserting that a market-wide move is not mistaken for
  stock-specific news, that a thin-history symbol yields no z-score rather
  than a garbage one, and that a reason string never claims "vs sector"
  when it used the NIFTY fallback, tests the decisions that could actually
  be wrong. Two tests are load-bearing for correctness elsewhere:
  `source_ts` stability while a price is unchanged (the precondition for
  dedup) and scenario timestamps never landing in the future (the bug that
  made events resurface forever). Both are regressions already hit once.
  No database required -- the valuable logic is pure.
- **Hour 35-38** — Migrated `Settings` from the deprecated class-based
  `Config` to `SettingsConfigDict`, clearing the only warning in the suite.
  A warning nobody clears is a warning nobody reads.
- **Hour 32-35** — `source_ts` and `fetched_at` are now two genuinely
  different things, closing the idempotency gap logged earlier. `source_ts`
  is when the PRICE is from (the provider's market timestamp) and is the
  dedup key; `fetched_at` is when WE polled. Collapsing them into poll time
  meant a redelivered quote got a fresh timestamp and was written again --
  the `(symbol, source_ts)` guard was correct but its input was not.
  Verified: ingesting unchanged quotes three times gives 48 new, then 0 new
  / 48 deduped, then 0 / 48.
- **Hour 32-35** — On conflict the row refreshes `fetched_at` only; price
  fields are immutable. Re-seeing a quote is not a new observation of a new
  price, but it IS evidence the pipeline is alive, and that belongs in
  `fetched_at`.
- **Hour 32-35** — The batch write became a single `INSERT ... SELECT
  unnest(...)` rather than executemany, because `RETURNING` is unavailable
  on executemany, and the per-row insert/update flag is what makes the
  dedup guarantee observable rather than merely asserted. Still one round
  trip.
- **Hour 32-35** — Staleness in the UI is keyed on `fetched_at`, not
  `source_ts`. A stock that has not traded for an hour is not stale data --
  it is an unchanged price, freshly confirmed. The earlier logic keyed on
  price age would have shown a red STALE badge on every quiet stock while
  polling was perfectly healthy, which is exactly the "market closed vs we
  don't know" confusion BUILD_PLAN.md section 9 warns about. The row now
  reads "quoted 65m ago · checked just now".
- **Hour 32-35** — A quote whose provider supplies no market timestamp is
  stored with confidence `unverified_ts` rather than silently falling back
  to poll time. Dedup cannot be guaranteed for such a row, and that should
  be visible in the data, not buried.
- **Hour 28-32** — A failing provider degrades, it does not fail over to a
  different data source. Rejected: falling back to a second provider (or to
  the replay feed) when the primary is down. Why: that hands the user a
  price which did not come from where they believe it came from. Letting
  the last real snapshot age and be labelled stale is the honest
  degradation, and it is what "staleness visible, never smoothed"
  (BUILD_PLAN.md section 9) actually requires. Nothing is invented to
  fill a gap.
- **Hour 28-32** — Circuit breaker on the provider: 3 consecutive failures
  opens it for 60s, then a single half-open probe decides whether to close
  or re-open. Without it, one dead upstream means 48 doomed HTTP calls per
  ingest cycle, every cycle -- slow, noisy, and more likely to get the
  scraper rate-limited into a longer outage. Verified: during a simulated
  outage the cycle stops after 3 attempts and a second cycle makes zero
  calls at all.
- **Hour 28-32** — The breaker and chaos switch live in a wrapper that
  `get_provider()` always applies, rather than being something each call
  site opts into. A resilience guard that a code path can forget to use
  is not a guard.
- **Hour 28-32** — Failure isolation is per-symbol (one bad ticker skips,
  the cycle continues) but breaker-open aborts the whole cycle. The two
  cases are genuinely different: one sick symbol is normal, a dead
  provider is not, and retrying it 47 more times helps nobody.
- **Hour 19-28** — Universe expanded from 10 hand-picked symbols to the
  NIFTY 50, and replay prices are now *generated* from each symbol's real
  last close and 20-day average volume (`replay_baseline.json`, written by
  the seed script) rather than hand-written. Only the few symbols that
  actually act in the demo scenario keep hand-authored scripts. Rejected:
  hand-writing a plausible price per ticker. Why: it does not scale past a
  handful, and getting one wrong silently flags that stock as breaching
  its 52-week range on every tick -- a bug already hit once at 10 symbols.
  A symbol resting at its own last close has 0% change and 1.0x volume, so
  it scores near zero and correctly stays quiet.
- **Hour 19-28** — `last_close` takes the last *settled* close
  (`closes.dropna().iloc[-1]`). The naive `.iloc[-1]` picks up today's
  unclosed bar, whose Close is NaN: 11 of 48 symbols silently got a NaN
  resting price, and `json.dumps` wrote a literal `NaN` -- which is invalid
  JSON that strict parsers reject and Python reads back as a real float.
  The writer now passes `allow_nan=False` so this fails loudly instead.
- **Hour 19-28** — Seeding fetches first and writes second: a symbol only
  reaches the `symbols` table once a usable baseline exists for it. The
  earlier order inserted symbol rows up front, so a ticker that failed to
  resolve left an orphan row with no baseline (hit once with TATAMOTORS).
- **Hour 19-28** — The digest reports `flagged_count` / `watched_count`,
  not just `suppressed_count`. Restraint is only legible against the size
  of what was checked: "1 of 40 broke pattern, the other 39 stayed quiet"
  is the claim; a suppressed count of 0 looks like nothing happened.
- **Hour 19-28** — Demo controls fast-forward the replay feed's *clock*
  rather than injecting events. `/demo/run-scenario` pins the feed to each
  scripted minute and runs a real ingest + detection pass at each one, so
  the detection layer reaches its own conclusions from the same quotes it
  would have seen live. Rejected: seeding rows straight into `events`.
  Why: injected events prove nothing about the pipeline, and the first
  Q&A question would be "so does the detection actually work?"
- **Hour 19-28** — A replayed scenario is anchored to end *now*, mapping
  scripted minutes onto the recent past. The first version stamped them
  forward from server start, putting quotes up to an hour in the future --
  and a future timestamp is never "since you last looked", so the event
  resurfaced on every single refresh no matter how many times it was read.
  This is the plan's own "watermark in the future (clock skew)" edge case,
  found by testing the read-twice path rather than by reasoning about it.
- **Hour 19-28** — `source_ts` is the OBSERVATION time in both providers,
  not the time the price last changed. A quiet stock polled just now is
  fresh data, not stale data; stamping the last-change time made untouched
  symbols look increasingly stale while polling was in fact healthy.
  Known consequence to revisit: because yfinance's adapter also stamps
  poll time, a genuine redelivery of the same upstream quote gets a new
  `source_ts` and is not deduped by the `(symbol, source_ts)` key. The
  proper fix is to use the provider's own market timestamp
  (`regularMarketTime`) as `source_ts`; the idempotency guard is correct,
  its input is not yet.
- **Hour 19-28** — Snapshot writes are batched into one statement per
  ingest cycle instead of one per symbol. Against a remote Neon instance
  the round trip, not the insert, is what scales with watchlist size:
  the demo scenario went 40s -> 13s on the same work.
- **Hour 19-28** — An event's headline (score, reason, kind) is its PEAK
  reading, not its most recent one. Extending an event used to overwrite
  the headline every cycle, so a move that spiked hard and then eased off
  reported itself with its weakest numbers (`z=+2.8 | 1.7x`) instead of
  what actually made it significant (`z=+4.5 | 3.2x`). Peak/trough and
  `last_updated_ts` still track every extension.
- **Hour 15-19** — Digest fetch itself advances the read watermark (no
  separate "mark as read" call) -- checking the digest IS the act of
  reading, matching the plan's "unread inbox" framing. Caught and fixed a
  real bug while writing this: the watermark must be READ before it's
  advanced, since advancing first would make every visit look like a
  first visit. Advance is still monotonic (`GREATEST`) so two concurrent
  reads can't move it backward.
