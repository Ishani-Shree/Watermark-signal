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
