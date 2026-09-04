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
