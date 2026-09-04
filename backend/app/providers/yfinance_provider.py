from datetime import datetime, timezone

import yfinance as yf

from .base import PriceProvider, Quote


class YFinanceProvider(PriceProvider):
    def get_latest(self, symbol: str) -> Quote | None:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        if not info or info.get("lastPrice") is None:
            return None
        return Quote(
            symbol=symbol,
            price=float(info["lastPrice"]),
            volume=int(info.get("lastVolume") or 0),
            prev_close=float(info.get("previousClose") or info["lastPrice"]),
            source_ts=datetime.now(timezone.utc),
            source="yfinance",
        )

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{days}d")
        quotes = []
        for ts, row in hist.iterrows():
            quotes.append(
                Quote(
                    symbol=symbol,
                    price=float(row["Close"]),
                    volume=int(row["Volume"]),
                    prev_close=float(row["Close"]),
                    source_ts=ts.to_pydatetime(),
                    source="yfinance",
                )
            )
        return quotes
