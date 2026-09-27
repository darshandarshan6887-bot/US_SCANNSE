"""Download the official index constituent lists from niftyindices.com into shared-data/index_lists.json.

    python update_index_lists.py

enrich_stocks.py uses that file automatically (falling back to the built-in approximate lists).
Lists that fail to download keep their previous content. Re-run after NSE's March/September rebalancing.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from typing import Callable, Dict, List

import pandas as pd

import config
from common import atomic_write_text, get_logger, norm_symbol

log = get_logger("index_lists")
BASE = "https://www.niftyindices.com/IndexConstituent/"
FILES = {
    "Nifty 50": "ind_nifty50list.csv", "Nifty Next 50": "ind_niftynext50list.csv",
    "Nifty 100": "ind_nifty100list.csv", "Nifty Midcap 150": "ind_niftymidcap150list.csv",
    "Nifty Smallcap 250": "ind_niftysmallcap250list.csv", "Nifty 500": "ind_nifty500list.csv",
    "Nifty IT": "ind_niftyitlist.csv", "Nifty Bank": "ind_niftybanklist.csv",
    "Nifty Pharma": "ind_niftypharmalist.csv", "Nifty Auto": "ind_niftyautolist.csv",
    "Nifty FMCG": "ind_niftyfmcglist.csv", "Nifty Metal": "ind_niftymetallist.csv",
    "Nifty Energy": "ind_niftyenergylist.csv", "Nifty Financial Services": "ind_niftyfinancelist.csv",
    "Nifty Realty": "ind_niftyrealtylist.csv", "Nifty PSU Bank": "ind_niftypsubanklist.csv",
    "Nifty Infrastructure": "ind_niftyinfralist.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def fetch_text(url: str) -> str:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def parse_symbols(text: str) -> List[str]:
    df = pd.read_csv(StringIO(text), dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    if "SYMBOL" not in df.columns:
        raise ValueError(f"no Symbol column: {list(df.columns)}")
    return sorted({norm_symbol(x) for x in df["SYMBOL"].dropna()})


def main(fetch: Callable[[str], str] = fetch_text) -> int:
    current: Dict[str, List[str]] = {}
    if config.INDEX_LISTS_JSON.exists():
        try:
            current = json.loads(config.INDEX_LISTS_JSON.read_text(encoding="utf-8"))
        except ValueError:
            current = {}
    ok = 0
    for name, fname in FILES.items():
        try:
            syms = parse_symbols(fetch(BASE + fname))
            if len(syms) < 5:
                raise ValueError("too few symbols")
            current[name] = syms
            ok += 1
            log.info(f"  {name}: {len(syms)}")
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"  {name}: FAILED ({type(e).__name__}: {str(e)[:70]}) - keeping previous")
    if ok:
        atomic_write_text(config.INDEX_LISTS_JSON, json.dumps(current, indent=1, ensure_ascii=False))
        log.info(f"Saved {config.INDEX_LISTS_JSON} ({ok}/{len(FILES)} lists refreshed). Now run: python enrich_stocks.py --indices-only")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
