from datetime import datetime, timezone

import yfinance as yf

from .base import PriceProvider, Quote


def _frame_for(df, symbol: str):
    """yfinance returns a flat frame for one ticker and a column-grouped one
    for several. Normalise so callers do not care which."""
    try:
        return df[symbol]
    except (KeyError, TypeError):
        return df


class YFinanceProvider(PriceProvider):
    """Live NSE data.

    Volume and previous close come from DAILY bars while price and timestamp
    come from MINUTE bars, and mixing them is deliberate:

    * `avg_volume_20d` in the baselines is an average of *daily* volume, so a
      quote's volume must be the day's cumulative figure. A one-minute bar
      shows ~635k against RELIANCE's 10.5M daily average -- every stock would
      permanently look like it had no volume, and the volume signal would go
      dead without ever erroring.
    * A daily bar's timestamp only changes once per day, which would collapse
      every poll into a single snapshot and erase the intraday path that
      revert detection depends on. The minute bar gives a real market
      timestamp that moves.
    """

    source_name = "yfinance"

    def get_latest(self, symbol: str) -> Quote | None:
        return self.get_latest_batch([symbol]).get(symbol)

    def get_latest_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """One request per resolution for the whole universe, rather than one
        per symbol. Measured: 48 symbols in ~2s batched, versus ~2.8s *each*
        going through `.info` -- and far fewer requests to be throttled on."""
        if not symbols:
            return {}

        fetched_at = datetime.now(timezone.utc)
        daily = yf.download(
            symbols, period="5d", interval="1d",
            progress=False, auto_adjust=False, group_by="ticker",
        )
        intraday = yf.download(
            symbols, period="1d", interval="1m",
            progress=False, auto_adjust=False, group_by="ticker",
        )

        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            quote = self._build_quote(symbol, daily, intraday, fetched_at)
            if quote is not None:
                quotes[symbol] = quote
        return quotes

    @staticmethod
    def _build_quote(symbol, daily, intraday, fetched_at) -> Quote | None:
        try:
            days = _frame_for(daily, symbol).dropna(subset=["Close"])
        except Exception:  # noqa: BLE001 - unofficial upstream, any shape is possible
            return None
        if days.empty:
            return None

        today = days.iloc[-1]
        volume = int(today["Volume"]) if today["Volume"] == today["Volume"] else 0
        # Second-to-last daily close. With only one session available there is
        # no prior close, so fall back to today's -- a 0% change, which is
        # honest about having nothing to compare against.
        prev_close = float(days["Close"].iloc[-2]) if len(days) >= 2 else float(today["Close"])

        price = float(today["Close"])
        source_ts = days.index[-1].to_pydatetime()

        # Prefer the minute bar: it is more current and its timestamp
        # actually moves, which is what makes the dedup key meaningful.
        try:
            minutes = _frame_for(intraday, symbol).dropna(subset=["Close"])
            if not minutes.empty:
                price = float(minutes["Close"].iloc[-1])
                source_ts = minutes.index[-1].to_pydatetime()
        except Exception:  # noqa: BLE001 - intraday is optional; daily already works
            pass

        if source_ts.tzinfo is None:
            source_ts = source_ts.replace(tzinfo=timezone.utc)

        return Quote(
            symbol=symbol,
            price=price,
            volume=volume,
            prev_close=prev_close,
            source_ts=source_ts.astimezone(timezone.utc),
            fetched_at=fetched_at,
            source="yfinance",
            has_market_ts=True,
        )

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        hist = yf.Ticker(symbol).history(period=f"{days}d")
        fetched_at = datetime.now(timezone.utc)
        return [
            Quote(
                symbol=symbol,
                price=float(row["Close"]),
                volume=int(row["Volume"]),
                prev_close=float(row["Close"]),
                source_ts=ts.to_pydatetime(),
                fetched_at=fetched_at,
                source="yfinance",
            )
            for ts, row in hist.iterrows()
        ]
