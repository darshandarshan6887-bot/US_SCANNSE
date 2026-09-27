"""Add one symbol.   python add_stock.py AAPL "Apple Inc"   (or run without arguments)"""
from __future__ import annotations

import argparse
import re
import sys

from common import get_logger, load_stocks, modify_stocks, norm_symbol, pipeline_lock, LockBusy
from update_universe import new_record

log = get_logger("add_stock")

_GROUP_RULES = [
    ("ETF", r"\bETF\b"), ("REIT", r"\bREIT\b"), ("FUND", r"\b(FUND|MUTUAL)\b"),
    ("DEBT", r"\b(BOND|NOTES|DEBENTURE|TREASURY)\b"),
]


def classify(name: str) -> str:
    up = str(name).upper()
    for group, pat in _GROUP_RULES:                               # whole-word matches
        if re.search(pat, up):
            return group
    return "EQUITY"


def yahoo_ok(symbol: str) -> bool:
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="5d", auto_adjust=False, raise_errors=True)
        return h is not None and not h.empty
    except Exception:                                             # noqa: BLE001
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--exchange", default="", help="e.g. NASDAQ, NYSE - optional, cosmetic only")
    ap.add_argument("--group", choices=["EQUITY", "ETF", "REIT", "INDEX", "DEBT", "FUND"])
    ap.add_argument("--no-validate", action="store_true", help="skip the Yahoo check")
    a = ap.parse_args(argv)
    sym = norm_symbol(a.symbol or input("Symbol (e.g. AAPL): "))
    name = (a.name or input("Company name: ")).strip() or sym
    if not sym:
        log.error("empty symbol"); return 1
    if not a.no_validate and not sym.startswith("^") and not yahoo_ok(sym):
        log.error(f"Yahoo has no data for {sym} - not added (use --no-validate to force)"); return 1
    group = a.group or classify(name)
    try:
        with pipeline_lock():
            def mutate(stocks):
                if any(norm_symbol(s["symbol"]) == sym for s in stocks):
                    return False
                stocks.append(new_record(sym, name, group, a.exchange)); return True
            added = modify_stocks(mutate)
    except LockBusy:
        log.error("Another pipeline run is in progress."); return 2
    log.info(f"{'Added' if added else 'Already present:'} {sym} ({group}). Run the daily sync to scan it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
