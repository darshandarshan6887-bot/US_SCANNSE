"""Sanity-check shared-data/stocks.json.   python validate_data.py   (exit code 1 if errors)"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

import config
from common import get_logger, load_stocks

log = get_logger("validate")
REQUIRED = ["symbol", "company_name", "instrument_group", "signal", "trend", "scanner_status", "current_price",
            "dashboard_url", "tradingview_url"]


def validate() -> int:
    errors, warns = [], []
    raw = config.STOCKS_JSON.read_text(encoding="utf-8")
    if re.search(r'[:\[,]\s*-?(NaN|Infinity)\b', raw):
        errors.append("file contains NaN/Infinity (browser JSON.parse will fail) - run any script once to rewrite it")
    try:
        json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as e:
        errors.append(f"not strict JSON: {e}")
    stocks = load_stocks()
    syms = Counter(s.get("symbol") for s in stocks)
    dup = [k for k, v in syms.items() if v > 1]
    if dup:
        errors.append(f"duplicate symbols: {dup[:10]}")
    for s in stocks:
        miss = [k for k in REQUIRED if k not in s]
        if miss:
            errors.append(f"{s.get('symbol')}: missing {miss}")
            if len(errors) > 30:
                break
    st = Counter(s.get("scanner_status") for s in stocks)
    log.info(f"{len(stocks)} records | 1H status {dict(st)}")
    log.info(f"groups: {dict(Counter(s.get('instrument_group') for s in stocks))}")
    stale = st.get("STALE", 0)
    if stale:
        warns.append(f"{stale} STALE scan results (Yahoo failed; previous analysis kept)")
    for w in warns:
        log.warning(w)
    for e in errors:
        log.error(e)
    log.info("VALID" if not errors else f"{len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(validate())
