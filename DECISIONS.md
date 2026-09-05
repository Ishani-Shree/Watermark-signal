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
