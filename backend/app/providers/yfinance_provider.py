from datetime import datetime, timezone

import yfinance as yf

from .base import PriceProvider, Quote


class YFinanceProvider(PriceProvider):
    def get_latest(self, symbol: str) -> Quote | None:
        ticker = yf.Ticker(symbol)
        fast = ticker.fast_info
        if not fast or fast.get("lastPrice") is None:
            return None

        fetched_at = datetime.now(timezone.utc)
        source_ts, has_market_ts = self._market_timestamp(ticker, fetched_at)

        return Quote(
            symbol=symbol,
            price=float(fast["lastPrice"]),
            volume=int(fast.get("lastVolume") or 0),
            prev_close=float(fast.get("previousClose") or fast["lastPrice"]),
            source_ts=source_ts,
            fetched_at=fetched_at,
            source="yfinance",
            has_market_ts=has_market_ts,
        )

    @staticmethod
    def _market_timestamp(ticker, fallback: datetime) -> tuple[datetime, bool]:
        """Yahoo's own timestamp for the quote, which is what makes
        `(symbol, source_ts)` a real dedup key: re-fetching an unchanged
        quote returns the same instant.

        It lives on `.info`, which is a heavier scrape than `fast_info`.
        That cost is the price of correct idempotency; the way to avoid
        paying it per symbol is a batch endpoint, not a fabricated
        timestamp. If it is unavailable we fall back to poll time and mark
        the row, because a fabricated source_ts silently disables dedup
        rather than announcing it.
        """
        try:
            epoch = ticker.info.get("regularMarketTime")
        except Exception:  # noqa: BLE001 - unofficial scrape, anything can surface
            epoch = None

        if epoch:
            return datetime.fromtimestamp(int(epoch), timezone.utc), True
        return fallback, False

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{days}d")
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
