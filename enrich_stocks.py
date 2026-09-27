"""Add market cap, sector, industry, P/E, 52-week range, volume and index memberships.

    python enrich_stocks.py                 # only stale/missing records
    python enrich_stocks.py --force         # everything
    python enrich_stocks.py --limit 100
    python enrich_stocks.py --indices-only  # recompute memberships + cap classes, no network
    python enrich_stocks.py --cap-method threshold

Safety: a failed/empty Yahoo answer NEVER overwrites data that was fetched earlier.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
from common import (LockBusy, by_symbol, fmt_ts, get_logger, is_auth_hiccup, is_retryable_yf_error,
                    load_stocks, modify_stocks, norm_symbol, now_market, parse_ts, pipeline_lock, retry,
                    safe_float, yf_cooldown_trigger, yf_cooldown_wait)

log = get_logger("enrich")


# ---------------------------------------------------------------------------
def load_index_map() -> Dict[str, frozenset]:
    """Reads shared-data/index_lists.json if you maintain one (e.g. S&P 500 / Nasdaq 100
    constituents). No index membership tagging happens without it - there is no NSE-style
    built-in fallback here, since that would mislabel US symbols with Indian index names."""
    if config.INDEX_LISTS_JSON.exists():
        try:
            data = json.loads(config.INDEX_LISTS_JSON.read_text(encoding="utf-8"))
            m = {k: frozenset(norm_symbol(x) for x in v) for k, v in data.items() if v}
            if m:
                return m
        except (OSError, ValueError):
            log.warning("index_lists.json unreadable - index memberships left empty")
    return {}


def memberships(symbol: str, index_map: Dict[str, frozenset]) -> List[str]:
    sym = norm_symbol(symbol)
    return sorted(name for name, members in index_map.items() if sym in members)


def classify_caps(stocks: List[Dict[str, Any]], method: str = config.CAP_METHOD) -> None:
    """Sets cap_category on every record.

    threshold : fixed US-dollar cut-offs from config.py - LARGE_CAP (>=$10B), MID_CAP (>=$2B),
                SMALL_CAP (>=$300M), MICRO_CAP (below that). This is the default.
    rank      : by market-cap rank among the tracked universe (1-100 LARGE, 101-250 MID, rest
                SMALL) - only 3 tiers, no MICRO_CAP, and relative to whatever you're tracking
                rather than fixed real-world cut-offs.
    """
    ranked = []
    for s in stocks:
        g = str(s.get("instrument_group", "")).upper()
        mc = safe_float(s.get("market_cap"))
        if g in config.NON_EQUITY_GROUPS:
            s["cap_category"] = g
        elif mc is None or mc <= 0:
            s["cap_category"] = "NA"
        elif method == "threshold":
            s["cap_category"] = ("LARGE_CAP" if mc >= config.LARGE_CAP_USD else
                                  "MID_CAP" if mc >= config.MID_CAP_USD else
                                  "SMALL_CAP" if mc >= config.SMALL_CAP_USD else "MICRO_CAP")
        else:
            ranked.append((mc, s))
    if method == "rank":
        ranked.sort(key=lambda t: -t[0])
        for i, (mc, s) in enumerate(ranked, start=1):
            s["cap_category"] = "LARGE_CAP" if i <= config.RANK_LARGE else "MID_CAP" if i <= config.RANK_MID else "SMALL_CAP"


def needs_enrichment(s: Dict[str, Any], force: bool, now: Optional[datetime] = None) -> bool:
    if force:
        return True
    now = now or now_market()
    status = s.get("enrich_status")
    if status is None:                                            # legacy record
        status = "OK" if s.get("market_cap") is not None else "NO_INFO"
    if status == "SKIPPED":
        return False
    raw = s.get("enriched_at") or s.get("enriched_date")
    try:
        when = parse_ts(raw) or datetime.strptime(str(raw)[:10], "%Y-%m-%d").replace(tzinfo=now.tzinfo)
    except (ValueError, TypeError):
        return True
    days = config.ENRICH_REFRESH_DAYS if status == "OK" else config.ENRICH_RETRY_DAYS
    return now - when >= timedelta(days=days)


def _yf_info(ticker: str) -> Dict[str, Any]:
    import yfinance as yf
    yf_cooldown_wait()
    t = yf.Ticker(ticker)
    info: Dict[str, Any] = {}
    try:
        info = t.get_info() or {}
    except Exception as e:                                        # noqa: BLE001
        if is_auth_hiccup(e):
            yf_cooldown_trigger()
        if is_retryable_yf_error(e):
            raise
    if not info.get("marketCap"):                                 # lighter endpoint as a fallback
        try:
            fi = t.fast_info
            info = dict(info)
            info.setdefault("marketCap", fi.get("market_cap"))
            info.setdefault("fiftyTwoWeekHigh", fi.get("year_high"))
            info.setdefault("fiftyTwoWeekLow", fi.get("year_low"))
            info.setdefault("averageVolume", fi.get("three_month_average_volume"))
        except Exception:                                         # noqa: BLE001
            pass
    return info


def enrich_one(stock: Dict[str, Any], info_fn=_yf_info) -> Dict[str, Any]:
    """Return the fields to merge. Empty dict + status NO_INFO means 'try again later'."""
    sym = norm_symbol(stock.get("symbol"))
    group = str(stock.get("instrument_group", "")).upper()
    if group in config.NON_EQUITY_GROUPS or sym.startswith("^"):
        return {"enrich_status": "SKIPPED"}
    try:
        info = retry(lambda: info_fn(sym), tries=config.MAX_RETRIES, base=3.0, retryable=is_retryable_yf_error)
    except Exception as e:                                        # noqa: BLE001
        return {"enrich_status": "NO_INFO", "enrich_error": f"{type(e).__name__}: {str(e)[:120]}"}
    mc = safe_float(info.get("marketCap"))
    found = {
        "market_cap": mc if mc else None,
        "sector": info.get("sector") or None,
        "industry": info.get("industry") or None,
        "pe_ratio": safe_float(info.get("trailingPE")),
        "week52_high": safe_float(info.get("fiftyTwoWeekHigh")),
        "week52_low": safe_float(info.get("fiftyTwoWeekLow")),
        "avg_volume": int(info["averageVolume"]) if safe_float(info.get("averageVolume")) is not None else None,
    }
    found = {k: v for k, v in found.items() if v is not None}
    if not found:
        return {"enrich_status": "NO_INFO", "enrich_error": "empty response"}
    found["enrich_status"] = "OK"
    return found


def merge(stock: Dict[str, Any], new: Dict[str, Any], stamp: str) -> None:
    for k, v in new.items():
        if k in ("enrich_status", "enrich_error"):
            continue
        stock[k] = v                                              # only non-empty values reach here
    stock["enrich_status"] = new["enrich_status"]
    if new.get("enrich_error"):
        stock["enrich_error"] = new["enrich_error"]
    else:
        stock.pop("enrich_error", None)
    stock["enriched_at"] = stamp
    stock["enriched_date"] = stamp[:10]
    for k, default in (("sector", "NA"), ("industry", "NA"), ("market_cap", None), ("pe_ratio", None),
                       ("week52_high", None), ("week52_low", None), ("avg_volume", None)):
        stock.setdefault(k, default)


def main(argv=None, info_fn=_yf_info) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=config.ENRICH_WORKERS)
    ap.add_argument("--indices-only", action="store_true")
    ap.add_argument("--cap-method", choices=["rank", "threshold"], default=config.CAP_METHOD)
    args = ap.parse_args(argv)
    try:
        with pipeline_lock():
            stocks = load_stocks()
            todo = [] if args.indices_only else [s for s in stocks if needs_enrichment(s, args.force)]
            if args.limit:
                todo = todo[:args.limit]
            log.info(f"{len(stocks)} records | to enrich: {len(todo)}")
            results: Dict[str, Dict[str, Any]] = {}
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {pool.submit(enrich_one, s, info_fn): norm_symbol(s.get("symbol")) for s in todo}
                for n, fut in enumerate(as_completed(futs), start=1):
                    results[futs[fut]] = fut.result()
                    if n % 100 == 0 or n == len(futs):
                        log.info(f"  {n}/{len(futs)}")
            imap = load_index_map()
            stamp = fmt_ts()

            def mutate(all_stocks):
                idx = by_symbol(all_stocks)
                for sym, new in results.items():
                    if sym in idx:
                        merge(idx[sym], new, stamp)
                for s in all_stocks:
                    s["index_memberships"] = memberships(s.get("symbol"), imap)
                classify_caps(all_stocks, args.cap_method)
            modify_stocks(mutate)

            c: Dict[str, int] = {}
            for r in results.values():
                c[r["enrich_status"]] = c.get(r["enrich_status"], 0) + 1
            log.info(f"Done in {time.time() - t0:.0f}s | " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
            fin = load_stocks()
            log.info(f"  market cap: {sum(1 for s in fin if s.get('market_cap') is not None)}/{len(fin)} | "
                     f"sector: {sum(1 for s in fin if s.get('sector') not in (None, 'NA', ''))}/{len(fin)}")
            caps: Dict[str, int] = {}
            for s in fin:
                caps[s.get("cap_category", "?")] = caps.get(s.get("cap_category", "?"), 0) + 1
            log.info("  caps: " + ", ".join(f"{k}={v}" for k, v in sorted(caps.items())))
            return 0
    except LockBusy:
        log.error("Another pipeline run is in progress.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
