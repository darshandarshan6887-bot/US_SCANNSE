# NSE Dashboard System (rebuilt)

A from-scratch, tested rewrite of the previous project. Same idea - scan the whole NSE
universe for market-structure signals (Smart Money Concepts: swing highs/lows, BOS/CHoCH,
Strong/Weak High/Low), on both daily and hourly candles - but with a corrected structure
engine, one dashboard code path for every stock, and a test suite that runs without touching
Yahoo Finance.

## What changed vs. the previous version, and why

| Area | Previous version | Here | Why |
|---|---|---|---|
| Structure engine | Processed pivots in *pivot* order; a superseded swing level could still fire BOS/CHoCH; occasionally used the wrong swing (see `tests/test_engine.py::test_interleaved_breaks_use_time_order`) | Single pass in *time* order; only the current, unbroken pivot can fire | Matches what the chart actually shows bar-by-bar; verified against a naive reference implementation on 400+ random series |
| Per-stock pages | ~3,270 generated HTML files, one per symbol, ~30 MB | One page, `stock/index.html?symbol=XXX` | Nothing to regenerate or go stale; identical content, 1/3000th the files |
| stocks.json / stocks_1h.json | Two independent write paths; a crash could leave them inconsistent, or leave `NaN`/`Infinity` in the file | One writer (`common.save_stocks`); `stocks_1h.json` is always *derived* from `stocks.json` in the same atomic write, and `json.dumps(..., allow_nan=False)` makes bad floats impossible to write | The dashboards fetch these files raw; invalid JSON breaks them |
| Price ticker | Bare symbol tried first, `.NS` second | `.NS` only | Dozens of NSE symbols (`TCS`, `STAR`, `SIS`, `MAZDA`, ...) are *also* real US tickers on Yahoo; the bare-symbol fallback could silently plot the wrong company |
| Concurrent writes (scan + prices + enrich) | Each script read-modified-wrote the whole file; a slow scan racing a price refresh could clobber the other's results | A single-writer file lock (`shared-data/.write.lock`) around every read-modify-write | Verified in `tests/test_pipeline.py::test_concurrent_edit_is_not_clobbered` |
| A failed Yahoo request | Sometimes wiped a symbol's last good scan | Kept, and flagged `scanner_status: STALE` | A network blip shouldn't erase real analysis |
| Windows console crash | `enrich_stocks.py` printed `→` and crashed on `cp1252` | `common.py` forces UTF-8 on stdout/stderr at import; batch files also set `PYTHONUTF8=1` | Belt and suspenders |
| Master pipeline | Stopped at the first failing step | Runs every requested step and reports all of them at the end | One broken step (e.g. NSE's site being down) shouldn't block the price refresh |
| Universe bootstrap | Two overlapping scripts (`bootstrap_full_nse_universe.py`, `bootstrap_all_nse_all_types.py`) | One script, `update_universe.py`, incremental (adds new listings, flags delisted ones, never blind-deletes) | Re-running a "bootstrap from scratch" script is destructive; NSE data changes daily |
| Testing | None found in the project | `tests/` - unit tests for the engine (against a reference implementation), the file store, freshness rules, the scan/price/enrich/universe pipelines (Yahoo mocked), plus a headless-browser check of both dashboard pages | So the next change can be verified without spending an hour scanning 3,000 stocks first |

## Layout

```
config.py            all tunable settings
common.py            time/IST clock, atomic strict-JSON file store, locking, retry, logging
smc_engine.py         the structure engine (pure functions, no I/O)
market_data.py        Yahoo Finance access (thread-safe)
index_data.py          static index-membership lists (fallback)
universe_data.py       static index/REIT/InvIT lists (fallback)
scan_market_structure_combined.py   1D + 1H structure scan
refresh_all_prices.py               fast batch price refresh
enrich_stocks.py                    sector / market cap / index membership / cap class
update_universe.py                  keep stocks.json in sync with NSE's official lists
update_index_lists.py               refresh the official index-constituent lists
add_stock.py                        add one symbol
validate_data.py                    sanity-check stocks.json
cleanup_legacy_child_pages.py       one-off: remove the old per-stock HTML folders
master_sync_all.py                  runs the pipeline in order
main-dashboard/index.html           the dashboard (unchanged UI; source pointed at the fixed data)
stock/index.html                    ONE generic per-stock page (1D/1H tabs) - replaces every child-*/
shared-data/stocks.json             the data (stocks_1h.json is generated, never edit it)
tests/                              unit + pipeline tests (no network required)
```

## First run

1. `install_requirements.bat`
2. `run_daily_scan.bat` - full sync (steps: scan -> prices -> enrich -> validate)
3. `start_dashboard.bat`, then open the page it prints/launches

If you're moving from the old project: keep your `shared-data/stocks.json` (drop it into
`shared-data/` here) - the schema is unchanged, and every script fills in anything missing on
its next run. You do **not** need the old `child-*` folders; `stock/index.html` replaces all of
them. If you kept them around, `cleanup_legacy_child_pages.py` will delete them.

## Every day / every hour

- **Daily** (after market close): `run_daily_scan.bat`
- **During market hours**: `run_hourly_scan.bat` - hourly scan only, a few minutes
- **Occasionally** (NSE adds/removes listings): `update_nse_universe.bat`
- **After March/September index rebalancing**: `python update_index_lists.py`, then
  `python enrich_stocks.py --indices-only`

## Command-line reference

```
python master_sync_all.py                              # scan, prices, enrich, validate
python master_sync_all.py --steps scan prices           # a subset, in the order given
python master_sync_all.py --force --workers 4            # gentler on Yahoo's rate limit

python scan_market_structure_combined.py                 # smart: only what is stale
python scan_market_structure_combined.py --timeframes 1h
python scan_market_structure_combined.py --symbols TCS INFY --force
python scan_market_structure_combined.py --limit 50       # quick smoke test

python refresh_all_prices.py [--limit N] [--batch 100]
python enrich_stocks.py [--force] [--indices-only] [--cap-method threshold]
python update_universe.py [--dry-run] [--prune]
python add_stock.py TCS "Tata Consultancy Services"
python validate_data.py
```

## Running the tests

```
python -m unittest discover -s tests -t .
```

No network access is used; Yahoo responses are supplied as in-memory pandas DataFrames.

## Design notes worth knowing

- **`scanner_status`** on every record is one of `PENDING_SCAN`, `OK`, `STALE` (Yahoo failed,
  showing the last good scan), `ERROR` (failed and no prior scan exists), `INSUFFICIENT_DATA`
  (fewer than `config.MIN_BARS` candles - typical for new listings, SME stocks, and most
  indices/ETFs on the hourly timeframe), `NO_PIVOTS` (flat/no-data price history).
- **Cap classification** (`config.CAP_METHOD`) defaults to `"rank"`: top 100 by market cap =
  Large, next 150 = Mid, rest = Small (SEBI/AMFI definition) - this changes as prices move,
  unlike a fixed rupee threshold. Switch to `"threshold"` in `config.py` if you'd rather have
  fixed cut-offs.
- **`NSE_DASH_HOME`** environment variable overrides where data/logs live, independent of
  where the `.py` files sit - this is how the test suite runs against a throwaway folder
  without touching your real `shared-data/`.
