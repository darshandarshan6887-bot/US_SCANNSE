"""Shared helpers: US market clock, strict JSON I/O, locks, logging, tickers."""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import config

# ---- Windows consoles default to cp1252 and crash on characters like "->" ----
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# TIME  (US equities trade in America/New_York, which observes DST - a fixed
# UTC offset is NOT correct here the way it was for IST, so we use zoneinfo).
# ---------------------------------------------------------------------------
MARKET_TZ = ZoneInfo(config.MARKET_TZ_NAME)
TS_FMT = "%Y-%m-%d %H:%M:%S"
TS_SUFFIX = " ET"


def now_market() -> datetime:
    return datetime.now(MARKET_TZ)


def fmt_ts(dt: Optional[datetime] = None) -> str:
    return (dt or now_market()).strftime(TS_FMT) + TS_SUFFIX


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM:SS ET' (or without the suffix) -> aware America/New_York datetime."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    for suffix in (TS_SUFFIX, " IST"):                    # " IST" tolerates records from before this pivot
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    try:
        return datetime.strptime(s, TS_FMT).replace(tzinfo=MARKET_TZ)
    except ValueError:
        return None


def _at(day: datetime, hm: tuple) -> datetime:
    return day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def is_market_open(now: Optional[datetime] = None) -> bool:
    now = now or now_market()
    return now.weekday() < 5 and _at(now, config.MARKET_OPEN) <= now < _at(now, config.MARKET_CLOSE)


# ---------------------------------------------------------------------------
# NUMBERS / JSON
# ---------------------------------------------------------------------------
def safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "NA"):
            return None
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def sanitize(obj: Any) -> Any:
    """NaN / +-Infinity are legal in Python's json but break the browser's JSON.parse."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):   # numpy scalar
        try:
            return sanitize(obj.item())
        except Exception:                                            # noqa: BLE001
            pass
    return obj


def dumps_strict(obj: Any, pretty: bool = False) -> str:
    kw = {"indent": 2} if pretty else {"separators": (",", ":")}
    return json.dumps(sanitize(obj), allow_nan=False, ensure_ascii=False, **kw)


def atomic_write_text(path, text: str) -> None:
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# LOCKING
# ---------------------------------------------------------------------------
class LockBusy(Exception):
    pass


@contextlib.contextmanager
def file_lock(path, wait: int = 0):
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise LockBusy(str(path))
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.remove(str(path))
        except OSError:
            pass


@contextlib.contextmanager
def pipeline_lock():
    if os.environ.get("NSE_PIPELINE_LOCKED"):    # master_sync_all.py already holds it
        yield
        return
    with file_lock(config.PIPELINE_LOCK, wait=0):
        yield


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(config.LOG_DIR / f"{name}_{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# RETRY / RATE LIMITING
# ---------------------------------------------------------------------------
def retry(fn: Callable, tries: int = config.MAX_RETRIES, base: float = 1.5,
          retryable: Callable[[Exception], bool] = lambda e: True, sleep=time.sleep):
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:                                   # noqa: BLE001
            last = e
            if not retryable(e) or i == tries - 1:
                raise
            sleep(base ** (i + 1) + (i * 0.37))
    raise last  # pragma: no cover


def is_rate_limit(e: Exception) -> bool:
    n = type(e).__name__
    msg = str(e).lower()
    return "RateLimit" in n or "429" in msg or "too many requests" in msg


def is_auth_hiccup(e: Exception) -> bool:
    """Yahoo's crumb/session token going bad mid-run (401 'Invalid Crumb' / 'Unauthorized').

    This is Yahoo's unofficial API having a flaky moment, not a code or credentials problem -
    it typically clears up on its own after a short pause.
    """
    msg = str(e).lower()
    return "invalid crumb" in msg or ("401" in msg and "unauthorized" in msg)


def is_retryable_yf_error(e: Exception) -> bool:
    return is_rate_limit(e) or is_auth_hiccup(e)


_YF_COOLDOWN_LOCK = threading.Lock()
_YF_COOLDOWN_UNTIL = 0.0


def yf_cooldown_wait() -> None:
    """Block until any cooldown set by yf_cooldown_trigger() has elapsed.

    Call this before every Yahoo request so threads that didn't personally hit the
    crumb error still pause instead of piling more bad requests onto the same broken session.
    """
    remaining = _YF_COOLDOWN_UNTIL - time.time()
    if remaining > 0:
        time.sleep(remaining)


def yf_cooldown_trigger(seconds: float = 20.0) -> None:
    """Called by a thread that just saw a crumb/auth error - pauses every worker briefly.

    A single crumb failure usually means Yahoo's session is bad for everyone right now, not
    just for this one symbol, so this stops the whole pool from thrashing while it resets.
    """
    global _YF_COOLDOWN_UNTIL
    with _YF_COOLDOWN_LOCK:
        _YF_COOLDOWN_UNTIL = max(_YF_COOLDOWN_UNTIL, time.time() + seconds)


# ---------------------------------------------------------------------------
# SYMBOLS / URLS / TICKERS
# ---------------------------------------------------------------------------
def norm_symbol(s: Any) -> str:
    return str(s or "").strip().upper()

# TradingView wants an exchange prefix (NASDAQ:AAPL, NYSE:KO, AMEX:SPY ...). We
# store the primary listing exchange per-record (see universe_data.py) and map
# it to TradingView's own exchange codes here.
_TV_EXCHANGE = {
    "NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSE AMERICAN": "AMEX", "NYSE ARCA": "AMEX",
    "AMEX": "AMEX", "BATS": "BATS", "CBOE": "BATS",
}


def tradingview_url(symbol: str, group: str = "", exchange: str = "") -> str:
    sym = quote(symbol.lstrip("^"), safe="")
    if symbol.startswith("^") or norm_symbol(group) == "INDEX":
        return f"https://www.tradingview.com/chart/?symbol={sym}"
    tv_ex = _TV_EXCHANGE.get(norm_symbol(exchange))
    return f"https://www.tradingview.com/chart/?symbol={tv_ex}:{sym}" if tv_ex else \
           f"https://www.tradingview.com/symbols/{sym}/"


def dashboard_url(symbol: str) -> str:
    return f"../stock/index.html?symbol={quote(symbol, safe='')}"


def yahoo_candidates(symbol: str, group: str = "") -> List[str]:
    """Ordered Yahoo tickers to try. US equities/ETFs/indices all use their plain
    ticker on Yahoo - no exchange suffix needed (unlike NSE's ".NS")."""
    s = norm_symbol(symbol)
    if not s:
        return []
    return [s]


# ---------------------------------------------------------------------------
# STOCK STORE  (stocks.json is the single source of truth; single 1H timeframe)
# ---------------------------------------------------------------------------
def load_stocks(path=None) -> List[Dict[str, Any]]:
    path = path or config.STOCKS_JSON
    if not os.path.exists(path):
        return []                               # brand-new repo: nothing tracked yet, not an error
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)                    # tolerates legacy NaN/Infinity tokens
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return data


def normalize_record(s: Dict[str, Any]) -> Dict[str, Any]:
    sym = norm_symbol(s.get("symbol"))
    s["symbol"] = sym
    s["tradingview_url"] = tradingview_url(sym, s.get("instrument_group", ""), s.get("exchange", ""))
    s["dashboard_url"] = dashboard_url(sym)
    return s


def save_stocks(stocks: List[Dict[str, Any]], pretty: bool = False) -> None:
    """Validate, de-duplicate, normalise, then write stocks.json atomically."""
    seen: Dict[str, Dict[str, Any]] = {}
    for s in stocks:
        normalize_record(s)
        if not s["symbol"]:
            continue
        seen[s["symbol"]] = s                  # last one wins
    clean = list(seen.values())
    atomic_write_text(config.STOCKS_JSON, dumps_strict(clean, pretty))


def modify_stocks(mutator: Callable[[List[Dict[str, Any]]], Any]) -> Any:
    """Short critical section: lock -> load fresh -> mutate -> save. Never clobbers other scripts."""
    with file_lock(config.WRITE_LOCK, wait=120):
        stocks = load_stocks()
        result = mutator(stocks)
        save_stocks(stocks)
        return result


def by_symbol(stocks: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {norm_symbol(s.get("symbol")): s for s in stocks}
