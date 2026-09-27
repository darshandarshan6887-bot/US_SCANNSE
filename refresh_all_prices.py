"""Quick price refresh: latest daily close for every symbol, downloaded in batches.

Also gives a price to symbols the scanner cannot analyse (new listings, SME, ETFs with short history).
Only `current_price` is touched here - never the scan results.

    python refresh_all_prices.py [--limit N] [--batch 100]
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

import config
from common import (LockBusy, fmt_ts, get_logger, load_stocks, modify_stocks, norm_symbol, pipeline_lock,
                    retry, yahoo_candidates, by_symbol)

log = get_logger("prices")


def extract_last_close(df: Optional[pd.DataFrame], ticker: str) -> Optional[float]:
    """Handle yfinance's MultiIndex layouts: (ticker, field), (field, ticker) and single-ticker frames."""
    if df is None or df.empty:
        return None
    sub = None
    if isinstance(df.columns, pd.MultiIndex):
        for lvl in range(df.columns.nlevels):
            if ticker in df.columns.get_level_values(lvl):
                sub = df.xs(ticker, axis=1, level=lvl)
                break
        if sub is None:
            return None
    else:
        sub = df
    if "Close" not in sub.columns:
        return None
    closes = pd.to_numeric(sub["Close"], errors="coerce").dropna()
    return float(closes.iloc[-1]) if len(closes) else None


def _download(tickers: List[str]):
    import yfinance as yf
    return yf.download(tickers, period="5d", interval="1d", auto_adjust=False, group_by="ticker",
                       threads=True, progress=False, timeout=config.REQUEST_TIMEOUT)


def fetch_prices(tickers: List[str], download=_download) -> Dict[str, Optional[float]]:
    """Prices for many tickers; batch first, single-ticker fallback for a batch that errored."""
    out: Dict[str, Optional[float]] = {}
    try:
        df = retry(lambda: download(tickers), tries=2, base=3.0)
        for t in tickers:
            out[t] = extract_last_close(df, t)
    except Exception as e:                                       # noqa: BLE001
        log.warning(f"batch of {len(tickers)} failed ({type(e).__name__}); retrying one by one")
        for t in tickers:
            try:
                out[t] = extract_last_close(download([t]), t)
            except Exception:                                    # noqa: BLE001
                out[t] = None
    return out


def main(argv=None, download=_download) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=config.PRICE_BATCH_SIZE)
    args = ap.parse_args(argv)
    try:
        with pipeline_lock():
            stocks = load_stocks()
            items: List[Tuple[str, str]] = []                    # (symbol, ticker)
            for s in stocks:
                sym = norm_symbol(s.get("symbol"))
                cands = yahoo_candidates(sym, s.get("instrument_group", ""))
                if sym and cands and s.get("listed") is not False:
                    items.append((sym, cands[0]))
            if args.limit:
                items = items[:args.limit]
            log.info(f"Refreshing prices for {len(items)} symbols in batches of {args.batch}")
            prices: Dict[str, Optional[float]] = {}
            t0 = time.time()
            for i in range(0, len(items), args.batch):
                chunk = items[i:i + args.batch]
                got = fetch_prices([t for _, t in chunk], download)
                for sym, t in chunk:
                    prices[sym] = got.get(t)
                log.info(f"  {min(i + args.batch, len(items))}/{len(items)}")

            stamp = fmt_ts()

            def mutate(all_stocks):
                idx = by_symbol(all_stocks)
                for sym, p in prices.items():
                    s = idx.get(sym)
                    if not s:
                        continue
                    if p is None:
                        s["price_status"] = "NO_DATA"            # keep the previous price
                    else:
                        s["current_price"] = str(round(p, 2))
                        s["price_status"] = "OK"
                        s["price_updated"] = stamp
            modify_stocks(mutate)
            ok = sum(1 for p in prices.values() if p is not None)
            log.info(f"Done in {time.time() - t0:.0f}s | updated {ok} | no data {len(prices) - ok}")
            return 0
    except LockBusy:
        log.error("Another pipeline run is in progress.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
