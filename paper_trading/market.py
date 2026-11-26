"""Timestamped, completed regular-session Finviz bars. Never synthesize a quote."""

import datetime as dt
import re
import threading
import time
from decimal import Decimal
from functools import lru_cache

import finviz_prices as fp
from finviz_agent import AgentError


class Problem(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9.-]{0,9}", value.strip().upper()
    ):
        raise Problem("Enter a valid US equity symbol.")
    return value.strip().upper()


class Market:
    def __init__(self, source):
        self.source = source
        self.cache = {}
        self.lock = threading.Lock()

    def history(self, ticker):
        ticker = symbol(ticker)
        with self.lock:
            cached = self.cache.get(ticker)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            if not self.source:
                raise Problem("Finviz intraday data is unavailable.", 503)
            try:
                raw = self.source.client.quote_history(ticker, "i1")
                now = dt.datetime.now(dt.UTC)
                bars = fp.minute_candles(fp.parse_intraday_csv(raw), 1, now, False)
                result = []
                for key in sorted(bars)[-500:]:
                    end = dt.datetime.fromisoformat(key).replace(
                        tzinfo=fp.ET
                    ) + dt.timedelta(minutes=1)
                    result.append(dict(bars[key], stamp=int(end.timestamp())))
                if not result:
                    raise ValueError("empty")
            except (AgentError, fp.FinvizPriceError, ValueError, OSError):
                raise Problem(
                    "No verified Finviz minute bars available.", 503
                ) from None
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[ticker] = (time.monotonic() + 20, result)
            return result

    def quote(self, ticker):
        bar = self.history(ticker)[-1]
        return {
            "price": str(Decimal(str(bar["c"]))),
            "stamp": bar["stamp"],
            "source": "Finviz 1-minute close",
        }


def fresh(quote, now):
    age = now - quote["stamp"]
    if (
        not 0 <= age <= 120
        or not open_minute(now)
        or not open_minute(quote["stamp"] - 1)
    ):
        raise Problem(
            "Reference price is stale or outside the NYSE regular session. Preview again when fresh data is available.",
            409,
        )


@lru_cache(maxsize=1)
def exchange():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS", side="left")


def open_minute(stamp):
    import pandas as pd

    minute = pd.Timestamp(stamp, unit="s", tz="UTC").floor("min")
    return exchange().is_open_on_minute(minute)
