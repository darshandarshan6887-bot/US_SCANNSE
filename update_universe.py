"""Keep stocks.json in sync with the official US-listed symbol directories.

    python update_universe.py              # add new listings, flag symbols no longer listed
    python update_universe.py --dry-run    # show what would change
    python update_universe.py --prune      # actually remove symbols flagged as unlisted
    python update_universe.py --min-price 10   # override the $5 floor for this run

Sources: nasdaqlisted.txt and otherlisted.txt, Nasdaq Trader's own daily symbol
directory (https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs) covering every
common stock and ETF listed on NASDAQ, NYSE, NYSE American, NYSE Arca and Cboe BZX.
This is a broad public proxy for "the US stocks/ETFs a retail platform would offer"
(for reference, CoinSwitch's own US-investing product lists ~5,500 tradable US
stocks/ETFs; that exact curated list isn't published anywhere we can pull from, so
this script instead builds the standard public universe most brokers draw from and
filters out test issues, warrants, units and rights, landing in a similar range).
If a download fails, that category is left untouched (nothing is flagged or removed
because of it) - same failure behaviour as the old NSE version.

Price floor (config.MIN_PRICE_USD, $5 by default):
  - Brand-new candidates get a one-off batch price check before being added; anything
    under the floor is skipped entirely (never enters stocks.json).
  - Symbols already being tracked are re-checked too, using the `current_price` field
    the daily EOD run already refreshes - no extra API calls needed for this part.
    Anything that has since fallen under the floor is excluded (listed=False,
    excluded_reason=LOW_PRICE) rather than deleted, so its history isn't lost; it's
    automatically re-included if the price recovers on a later run.

Two CSV snapshots are written to shared-data/ every run, purely for your own
inspection - the operational data always lives in stocks.json, not these CSVs:
  us_universe_all.csv       every candidate pulled from Nasdaq Trader, unfiltered
  us_universe_filtered.csv  the resulting tracked universe after the price floor
"""
from __future__ import annotations

import argparse
import csv
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

import universe_data as U
import smc_engine as eng
import config
from common import LockBusy, fmt_ts, get_logger, load_stocks, modify_stocks, norm_symbol, pipeline_lock, safe_float

log = get_logger("universe")

URLS = {
    "NASDAQ": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "OTHER": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
MIN_ROWS = {"NASDAQ": 2000, "OTHER": 1500}        # sanity check before trusting a list

# NYSE Trader's single-letter exchange codes on otherlisted.txt.
_OTHER_EXCHANGE = {"A": "NYSE AMERICAN", "N": "NYSE", "P": "NYSE ARCA", "Z": "BATS", "V": "IEXG"}
# Security-name substrings that mean "not a plain common stock/ETF" - warrants, units,
# rights and preferred shares aren't what "US stocks" means here.

# NOT excluding plain "Depositary Shares" - that would wrongly drop legitimate ADRs (BIDU,
# BABA, ASML...), which are ordinary common stock of foreign issuers; only their *preferred*
# variants (caught by "PREFERRED" below) are the kind we actually want filtered out.
_EXCLUDE_NAME_PATTERNS = ("WARRANT", " UNIT", " UNITS", " RIGHT", " RIGHTS", "PREFERRED", " NOTES")


def fetch_text(url: str) -> str:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def _to_yahoo_symbol(symbol: str) -> str:
    """Nasdaq Trader uses '.' for share classes (BRK.B); Yahoo/yfinance wants '-' (BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def _looks_tradeable(name: str, test_issue: str) -> bool:
    if norm_symbol(test_issue) == "Y":
        return False
    up = name.upper()
    return not any(p in up for p in _EXCLUDE_NAME_PATTERNS)


def parse_nasdaqlisted(text: str) -> List[Dict[str, str]]:
    out = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return out
    header = [c.strip() for c in lines[0].split("|")]
    idx = {c: i for i, c in enumerate(header)}
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        cols = line.split("|")
        if len(cols) != len(header):
            continue
        sym = cols[idx["Symbol"]].strip()
        name = cols[idx["Security Name"]].strip()
        test_issue = cols[idx.get("Test Issue", -1)] if "Test Issue" in idx else "N"
        is_etf = cols[idx.get("ETF", -1)] if "ETF" in idx else "N"
        if not sym or not _looks_tradeable(name, test_issue):
            continue
        out.append({"symbol": _to_yahoo_symbol(sym), "company_name": name,
                     "group": "ETF" if norm_symbol(is_etf) == "Y" else "EQUITY", "exchange": "NASDAQ"})
    return out


def parse_otherlisted(text: str) -> List[Dict[str, str]]:
    out = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return out
    header = [c.strip() for c in lines[0].split("|")]
    idx = {c: i for i, c in enumerate(header)}
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        cols = line.split("|")
        if len(cols) != len(header):
            continue
        sym = cols[idx["ACT Symbol"]].strip()
        name = cols[idx["Security Name"]].strip()
        exch = cols[idx.get("Exchange", -1)] if "Exchange" in idx else ""
        test_issue = cols[idx.get("Test Issue", -1)] if "Test Issue" in idx else "N"
        is_etf = cols[idx.get("ETF", -1)] if "ETF" in idx else "N"
        if not sym or not _looks_tradeable(name, test_issue):
            continue
        out.append({"symbol": _to_yahoo_symbol(sym), "company_name": name,
                     "group": "ETF" if norm_symbol(is_etf) == "Y" else "EQUITY",
                     "exchange": _OTHER_EXCHANGE.get(norm_symbol(exch), norm_symbol(exch) or "OTHER")})
    return out


def write_csv(rows: List[Dict[str, Any]], path, fields: List[str]) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    except OSError as e:                                          # noqa: BLE001
        log.warning(f"could not write {path}: {e}")


def new_record(symbol: str, name: str, group: str, exchange: str = "", price: Optional[float] = None) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "symbol": norm_symbol(symbol), "company_name": name, "instrument_group": group,
        "exchange": exchange, "is_regular_equity": group == "EQUITY",
        "last_updated": "NA", "added_at": fmt_ts(),
    }
    rec.update(eng.empty_fields("", "1H", "PENDING_SCAN"))         # sets current_price="NA" placeholder
    if price is not None:                                          # ...then overlay the real price we already have
        rec["current_price"] = str(round(price, 2))
        rec["price_status"] = "OK"
        rec["price_updated"] = fmt_ts()
    else:
        rec["price_status"] = "NO_DATA"
    return rec


def collect(fetch: Callable[[str], str] = fetch_text) -> Dict[str, Optional[List[Dict[str, str]]]]:
    """category -> list of {symbol, company_name, group, exchange}; None when the download/parse failed."""
    lists: Dict[str, Optional[List[Dict[str, str]]]] = {}
    parsers = {"NASDAQ": parse_nasdaqlisted, "OTHER": parse_otherlisted}
    for cat, url in URLS.items():
        try:
            rows = parsers[cat](fetch(url))
            if len(rows) < MIN_ROWS[cat]:
                raise ValueError(f"only {len(rows)} rows - looks wrong")
            lists[cat] = rows
            log.info(f"  {cat}: {len(rows)} tradeable symbols")
        except Exception as e:                                   # noqa: BLE001
            lists[cat] = None
            log.warning(f"  {cat}: download failed ({type(e).__name__}: {str(e)[:80]}) - category left unchanged")
    lists["INDEX"] = [{"symbol": x["symbol"], "company_name": x["company_name"], "group": "INDEX", "exchange": ""}
                       for x in U.US_INDICES]
    all_rows = [r for rows in lists.values() if rows for r in rows]
    write_csv(all_rows, config.UNIVERSE_RAW_CSV, ["symbol", "company_name", "group", "exchange"])
    log.info(f"  wrote {len(all_rows)} candidates to {config.UNIVERSE_RAW_CSV}")
    return lists


def plan_changes(stocks: List[Dict[str, Any]], lists) -> Dict[str, Any]:
    existing = {norm_symbol(s["symbol"]): s for s in stocks}
    add, unlist, relist = [], [], []
    # Which existing records a given source file is authoritative for, so a symbol from
    # otherlisted.txt is never compared against nasdaqlisted.txt's list (and vice versa).
    belongs_to = {
        "NASDAQ": lambda s: norm_symbol(s.get("exchange")) == "NASDAQ",
        "OTHER": lambda s: s.get("exchange") and norm_symbol(s.get("exchange")) != "NASDAQ"
        and s.get("instrument_group") != "INDEX",
    }
    for cat, rows in lists.items():
        if rows is None:
            continue
        seen = set()
        for r in rows:
            seen.add(r["symbol"])
            cur = existing.get(r["symbol"])
            if cur is None:
                add.append((cat, r))
            elif cur.get("listed") is False:
                relist.append(r["symbol"])
        match = belongs_to.get(cat)
        if match:                                                # only categories with a live official list
            for sym, s in existing.items():
                if match(s) and sym not in seen and s.get("listed") is not False:
                    unlist.append(sym)
    return {"add": add, "unlist": unlist, "relist": relist}


def price_check_candidates(add: List[Tuple[str, Dict[str, str]]], min_price: float,
                            fetch_prices=None) -> Tuple[List[Tuple[str, Dict[str, str], Optional[float]]], int]:
    """Batch-price every brand-new (non-index) candidate; drop anything under min_price.

    Returns (kept, dropped_count). Indices are always kept (they don't have a "price"
    in the same sense, and a $5 floor is meaningless for e.g. ^GSPC).
    """
    if fetch_prices is None:
        from refresh_all_prices import fetch_prices as _fp
        fetch_prices = _fp
    priced_syms = [r["symbol"] for _, r in add if r.get("group") != "INDEX"]
    prices = fetch_prices(priced_syms) if priced_syms else {}
    kept: List[Tuple[str, Dict[str, str], Optional[float]]] = []
    dropped = 0
    for cat, r in add:
        if r.get("group") == "INDEX":
            kept.append((cat, r, None))
            continue
        p = prices.get(r["symbol"])
        if p is None or p < min_price:
            dropped += 1
            continue
        kept.append((cat, r, p))
    return kept, dropped


def prune_low_priced(stocks: List[Dict[str, Any]], min_price: float) -> List[str]:
    """Re-check symbols already being tracked against their existing (daily-refreshed)
    current_price - no extra API calls. Returns the symbols newly excluded this run."""
    newly_excluded = []
    for s in stocks:
        if s.get("instrument_group") == "INDEX" or s.get("listed") is False:
            continue
        p = safe_float(s.get("current_price"))
        if p is not None and p < min_price:
            s["listed"] = False
            s["excluded_reason"] = "LOW_PRICE"
            newly_excluded.append(norm_symbol(s.get("symbol")))
        elif s.get("excluded_reason") == "LOW_PRICE" and p is not None and p >= min_price:
            s.pop("listed", None)                                 # price recovered - bring it back
            s.pop("excluded_reason", None)
    return newly_excluded


def apply_changes(stocks: List[Dict[str, Any]], priced_add, plan, prune: bool = False) -> List[str]:
    idx = {norm_symbol(s["symbol"]): s for s in stocks}
    for cat, r, price in priced_add:
        stocks.append(new_record(r["symbol"], r["company_name"], r["group"], r.get("exchange", ""), price))
    for sym in plan["relist"]:
        idx[sym].pop("listed", None)
        idx[sym].pop("excluded_reason", None)
    for sym in plan["unlist"]:
        idx[sym]["listed"] = False
        idx[sym]["excluded_reason"] = "DELISTED"
    if prune:
        stocks[:] = [s for s in stocks if s.get("listed") is not False]
    return prune_low_priced(stocks, config.MIN_PRICE_USD)


def main(argv=None, fetch=fetch_text, fetch_prices=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true", help="delete symbols flagged as unlisted")
    ap.add_argument("--min-price", type=float, default=config.MIN_PRICE_USD)
    args = ap.parse_args(argv)
    try:
        with pipeline_lock():
            lists = collect(fetch)
            plan = plan_changes(load_stocks(), lists)
            log.info(f"new: {len(plan['add'])} | no longer listed: {len(plan['unlist'])} | re-listed: {len(plan['relist'])}")
            priced_add, dropped = price_check_candidates(plan["add"], args.min_price, fetch_prices)
            log.info(f"new candidates priced: {len(priced_add)} kept, {dropped} dropped (under ${args.min_price:g})")
            for cat, r, p in priced_add[:25]:
                log.info(f"  + {r['symbol']:<10} {cat:<7} ${p:.2f}" if p is not None else f"  + {r['symbol']:<10} {cat:<7} (index)")
            for sym in plan["unlist"][:25]:
                log.info(f"  - {sym}")
            if args.dry_run:
                return 0
            newly_excluded = modify_stocks(lambda stocks: apply_changes(stocks, priced_add, plan, args.prune))
            if newly_excluded:
                log.info(f"newly excluded for falling under ${args.min_price:g}: {len(newly_excluded)} "
                         f"({', '.join(newly_excluded[:15])}{'...' if len(newly_excluded) > 15 else ''})")
            filtered_rows = [s for s in load_stocks() if s.get("listed") is not False]
            write_csv(filtered_rows, config.UNIVERSE_FILTERED_CSV,
                      ["symbol", "company_name", "instrument_group", "exchange", "current_price"])
            log.info(f"wrote {len(filtered_rows)} tracked symbols to {config.UNIVERSE_FILTERED_CSV}")
            log.info("stocks.json updated. Run the sync pipeline to scan the new symbols.")
            return 0
    except LockBusy:
        log.error("Another pipeline run is in progress.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
