"""Central settings. Everything tunable lives here."""
import os
from pathlib import Path

# NSE_DASH_HOME (kept for backward compat with tests) lets you point the whole
# system at another folder.
CODE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("NSE_DASH_HOME") or CODE_DIR)
DATA_DIR = BASE_DIR / "shared-data"
LOG_DIR = BASE_DIR / "logs"
STOCKS_JSON = DATA_DIR / "stocks.json"          # 1H data lives directly in here now - single timeframe
INDEX_LISTS_JSON = DATA_DIR / "index_lists.json"  # optional; empty/absent = no index-membership tagging
PIPELINE_LOCK = DATA_DIR / ".pipeline.lock"
WRITE_LOCK = DATA_DIR / ".write.lock"

# ---- Market-structure engine (same defaults as before) ----------------------
SWING_LEN = 50                    # LuxAlgo "swings length"
MIN_BARS = SWING_LEN * 2 + 20     # fewer bars than this -> INSUFFICIENT_DATA
HOURLY_LOOKBACK_DAYS = 729        # Yahoo serves at most ~730 days of 1h data
HOURLY_DATE_FMT = "%Y-%m-%d %H:%M"

# ---- Market clock (US equities: NYSE/NASDAQ, America/New_York - has DST) ----
MARKET_TZ_NAME = "America/New_York"
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)
# The pipeline runs every 6h regardless of the session, so "freshness" is just
# "has it been re-scanned recently" rather than "has today's session closed".
SCAN_REFRESH_HOURS = 5            # rescan a symbol if its last 1H scan is older than this

# ---- Downloading ------------------------------------------------------------
SCAN_WORKERS = 8
ENRICH_WORKERS = 6
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BASE = float(os.environ.get("NSE_RETRY_BASE", 2.0))   # seconds; exponential backoff base
PRICE_BATCH_SIZE = 100
CHECKPOINT_EVERY = 400            # save partial scan results every N symbols

# ---- Cap classification (US market-cap tiers, in plain dollars) ------------
CAP_METHOD = "threshold"          # "threshold" (fixed $ cut-offs, gives 4 tiers incl. MICRO_CAP)
                                   # or "rank" (top100/next150/rest of the tracked universe - only 3 tiers)
LARGE_CAP_USD = 10_000_000_000     # >= $10B
MID_CAP_USD = 2_000_000_000        # >= $2B
SMALL_CAP_USD = 300_000_000        # >= $300M; below this -> MICRO_CAP
RANK_LARGE = 100
RANK_MID = 250
ENRICH_REFRESH_DAYS = 30          # enrich only runs ~monthly now, so treat anything under a month as fresh
ENRICH_RETRY_DAYS = 5
MIN_PRICE_USD = 5.0                # penny-stock floor - applied when adding new symbols AND when pruning
                                    # existing ones whose price has since dropped below it (see update_universe.py)
UNIVERSE_RAW_CSV = DATA_DIR / "us_universe_all.csv"        # every candidate pulled from Nasdaq Trader, unfiltered
UNIVERSE_FILTERED_CSV = DATA_DIR / "us_universe_filtered.csv"  # what's actually tracked after the $5 floor

NON_EQUITY_GROUPS = {"INDEX", "ETF", "FUND", "DEBT", "REIT", "INVIT"}
