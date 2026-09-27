import json
import numpy as np
import pandas as pd

import config
import smc_engine as eng
from common import save_stocks
from update_universe import new_record


def synth(seed, n=400, hourly=False):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    h = c * (1 + np.abs(rng.normal(0, 0.005, n)))
    l = c * (1 - np.abs(rng.normal(0, 0.005, n)))
    o = c
    if hourly:
        idx = pd.date_range("2025-01-01 09:15", periods=n, freq="h")
    else:
        idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}, index=idx)


def seed_file(symbols, groups=None):
    """Write a fresh stocks.json with pending records."""
    recs = [new_record(s, f"{s} Ltd", "EQ", (groups or {}).get(s, "EQUITY")) for s in symbols]
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_stocks(recs)
    return recs


def read():
    return json.loads(config.STOCKS_JSON.read_text(encoding="utf-8"))
