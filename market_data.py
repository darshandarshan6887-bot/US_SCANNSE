"""Yahoo Finance access. One place, thread-safe (Ticker.history, not the global-state yf.download)."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

import pandas as pd

import config
from common import MARKET_TZ, now_market, retry, yahoo_candidates

_OHLC = ["Open", "High", "Low", "Close"]


@dataclass
class Fetch:
    status: str                       # OK | NO_DATA | ERROR
    df: Optional[pd.DataFrame] = None
    ticker: str = ""
    error: str = ""


def normalize(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """-> DataFrame[Date, Open, High, Low, Close], tz-naive America/New_York, sorted, unique, no NaN."""
    if raw is None or len(raw) == 0:
        return None
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if not all(c in df.columns for c in _OHLC):
        return None
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(MARKET_TZ).tz_localize(None)
    out = df[_OHLC].apply(pd.to_numeric, errors="coerce")
    out.insert(0, "Date", idx)
    out = out.dropna().reset_index(drop=True)
    out = out.drop_duplicates(subset="Date", keep="last").sort_values("Date").reset_index(drop=True)
    return out if len(out) else None


def _history(ticker: str, interval: str, start: str):
    import yfinance as yf                                  # imported lazily so tests can run without it
    t = yf.Ticker(ticker)
    kw = dict(start=start, interval=interval, auto_adjust=False, actions=False, timeout=config.REQUEST_TIMEOUT)
    try:
        return t.history(raise_errors=True, **kw)
    except TypeError:                                      # very old yfinance without raise_errors
        return t.history(**kw)


def _is_no_data(e: Exception) -> bool:
    n = type(e).__name__
    return any(k in n for k in ("PricesMissing", "TickerMissing", "TzMissing", "InvalidPeriod")) or \
        "delisted" in str(e).lower() or "no data found" in str(e).lower() or "no price data" in str(e).lower()


def fetch_one(ticker: str, interval: str, lookback_days: int, history_fn=None) -> Fetch:
    history_fn = history_fn or _history                   # looked up at call time (patchable)
    start = (now_market() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    def call():
        return history_fn(ticker, interval, start)

    try:
        raw = retry(call, tries=config.MAX_RETRIES, base=config.RETRY_BASE,
                    retryable=lambda e: not _is_no_data(e),
                    sleep=lambda s: time.sleep(s + random.uniform(0, config.RETRY_BASE)))
    except Exception as e:                                 # noqa: BLE001
        if _is_no_data(e):
            return Fetch("NO_DATA", ticker=ticker, error=str(e)[:160])
        return Fetch("ERROR", ticker=ticker, error=f"{type(e).__name__}: {str(e)[:160]}")
    df = normalize(raw)
    if df is None:
        return Fetch("NO_DATA", ticker=ticker, error="empty history")
    return Fetch("OK", df=df, ticker=ticker)


def fetch_history(symbol: str, group: str, interval: str, lookback_days: int, history_fn=None) -> Fetch:
    """Try each Yahoo candidate for the symbol. ERROR wins over NO_DATA so transient failures are visible."""
    last = Fetch("NO_DATA", error="no candidates")
    saw_error: Optional[Fetch] = None
    for ticker in yahoo_candidates(symbol, group):
        r = fetch_one(ticker, interval, lookback_days, history_fn)
        if r.status == "OK":
            return r
        if r.status == "ERROR":
            saw_error = r
        last = r
    return saw_error or last
