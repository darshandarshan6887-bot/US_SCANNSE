"""Run the whole pipeline in order.  python master_sync_all.py [options]

Steps (default): scan -> prices -> enrich -> validate
  scan      1D + 1H market structure (smart freshness rules)     scan_market_structure_combined.py
  prices    latest close for every symbol (also unscannable ones) refresh_all_prices.py
  enrich    sector / market cap / indices / cap class              enrich_stocks.py
  universe  add new NSE listings (not in the default run)          update_universe.py
  validate  sanity check                                            validate_data.py

A failing step does not stop the others (the old version aborted at the first failure). The exit
code is 1 if any step failed, and a summary table is printed at the end.

    python master_sync_all.py --steps scan prices        # subset
    python master_sync_all.py --steps universe scan prices enrich validate
    python master_sync_all.py --force                    # forwarded to the scanner
    python master_sync_all.py --limit 50                 # forwarded to scan/prices/enrich
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import config
from common import LockBusy, get_logger, pipeline_lock

log = get_logger("master")
STEPS = {
    "universe": "update_universe.py",
    "scan": "scan_market_structure_combined.py",
    "prices": "refresh_all_prices.py",
    "enrich": "enrich_stocks.py",
    "validate": "validate_data.py",
}
DEFAULT = ["scan", "prices", "enrich", "validate"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", nargs="+", choices=list(STEPS), default=DEFAULT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        with pipeline_lock():
            results = []
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NSE_PIPELINE_LOCKED="1")
            for name in [s for s in STEPS if s in a.steps]:
                cmd = [sys.executable, str(config.CODE_DIR / STEPS[name])]
                if a.force and name in ("scan", "enrich"):
                    cmd.append("--force")
                if a.limit and name in ("scan", "prices", "enrich"):
                    cmd += ["--limit", str(a.limit)]
                if a.workers and name in ("scan", "enrich"):
                    cmd += ["--workers", str(a.workers)]
                log.info(f"=== {name}: {' '.join(cmd[1:])}")
                t0 = time.time()
                rc = subprocess.call(cmd, cwd=str(config.CODE_DIR), env=env)
                results.append((name, rc, time.time() - t0))
                log.info(f"=== {name}: {'OK' if rc == 0 else f'FAILED (exit {rc})'}  {time.time() - t0:.0f}s")
            log.info("-" * 44)
            for name, rc, dt in results:
                log.info(f"  {name:<9} {'OK' if rc == 0 else 'FAILED':<7} {dt:6.0f}s")
            return 1 if any(rc for _, rc, _ in results) else 0
    except LockBusy:
        log.error("Another pipeline run is in progress (shared-data/.pipeline.lock). Wait, or delete that file if a previous run crashed.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
