import json
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from tests import helpers
import config
import common
import market_data
import scan_market_structure_combined as scan
import refresh_all_prices as refresh
import enrich_stocks as enrich
import update_universe as uni


def fake_history(mapping):
    """mapping: ticker -> DataFrame | Exception | 'empty'"""
    def fn(ticker, interval, start):
        v = mapping.get(ticker)
        if isinstance(v, Exception):
            raise v
        if v is None or (isinstance(v, str) and v == "empty"):
            raise type("YFPricesMissingError", (Exception,), {})(f"{ticker}: possibly delisted; no price data found")
        return v(interval) if callable(v) else v
    return fn


class Store(unittest.TestCase):
    def test_strict_json_and_derived_view(self):
        recs = helpers.seed_file(["AAA", "BBB", "AAA"])
        recs[0]["pe_ratio"] = float("nan"); recs[0]["x"] = float("inf")
        recs[1]["signal_1h"] = "STRONG_LOW"; recs[1]["signal"] = "NO_SIGNAL"
        common.save_stocks(recs)
        raw = config.STOCKS_JSON.read_text(encoding="utf-8")
        self.assertNotIn("NaN", raw); self.assertNotIn("Infinity", raw)
        data = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        self.assertEqual(len(data), 2)                                  # duplicate dropped
        h = json.loads(config.STOCKS_1H_JSON.read_text(encoding="utf-8"))
        self.assertEqual({r["symbol"]: r["signal"] for r in h}["BBB"], "STRONG_LOW")
        self.assertTrue(all(not k.endswith("_1h") for r in h for k in r))
        self.assertEqual(data[0]["dashboard_url"], "../stock/index.html?symbol=AAA")

    def test_url_encoding(self):
        self.assertEqual(common.dashboard_url("M&M"), "../stock/index.html?symbol=M%26M")
        self.assertEqual(common.yahoo_candidates("TCS"), ["TCS.NS"])            # no bare US-ticker fallback

    def test_lock(self):
        with common.file_lock(config.WRITE_LOCK):
            with self.assertRaises(common.LockBusy):
                with common.file_lock(config.WRITE_LOCK, wait=0):
                    pass
        with common.file_lock(config.WRITE_LOCK):
            pass


class Freshness(unittest.TestCase):
    def ist(self, s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=common.IST)

    def test_rules(self):
        s = {"scanned_at": "2026-09-21 10:00:00 IST", "scanned_at_1h": "2026-09-21 10:00:00 IST"}
        mid = self.ist("2026-09-21 11:00")                       # Monday, market open
        self.assertFalse(scan.needs_scan(s, "1d", mid))          # daily done this session
        self.assertTrue(scan.needs_scan(s, "1h", mid))           # hourly: always while open
        after = self.ist("2026-09-21 16:30")
        self.assertTrue(scan.needs_scan(s, "1d", after))         # final candle not yet captured
        s2 = {"scanned_at": "2026-09-21 16:10:00 IST", "scanned_at_1h": "2026-09-21 16:10:00 IST"}
        self.assertFalse(scan.needs_scan(s2, "1d", after)); self.assertFalse(scan.needs_scan(s2, "1h", after))
        self.assertTrue(scan.needs_scan(s2, "1d", after, force=True))
        sat = self.ist("2026-09-26 12:00")
        s3 = {"scanned_at": "2026-09-25 16:00:00 IST"}
        self.assertFalse(scan.needs_scan(s3, "1d", sat))
        self.assertTrue(scan.needs_scan({}, "1d", sat))


class Scanner(unittest.TestCase):
    def setUp(self):
        self.syms = ["OKONE", "OKTWO", "SHORT", "GONE"]
        helpers.seed_file(self.syms)

    def hist(self, extra=None):
        m = {"OKONE.NS": helpers.synth(1, 400), "OKTWO.NS": helpers.synth(2, 400),
             "SHORT.NS": helpers.synth(3, 40)}                  # too short; GONE.NS missing -> no data
        m.update(extra or {})
        return m

    def run_scan(self, mapping, tfs=("1d", "1h")):
        stocks = common.load_stocks()
        targets = list(stocks)
        scan.run_scan(targets, lambda s: list(tfs), workers=3, history_fn=fake_history(mapping), progress_every=1000)
        return {s["symbol"]: s for s in helpers.read()}

    def test_full_scan_states(self):
        d = self.run_scan(self.hist())
        self.assertEqual(d["OKONE"]["scanner_status"], "OK")
        self.assertEqual(d["OKONE"]["scanner_status_1h"], "OK")
        self.assertEqual(d["OKONE"]["timeframe"], "1D"); self.assertEqual(d["OKONE"]["timeframe_1h"], "1H")
        self.assertIn(d["OKONE"]["trend"], ("BULLISH", "BEARISH", "UNKNOWN"))
        self.assertEqual(d["SHORT"]["scanner_status"], "INSUFFICIENT_DATA")
        self.assertEqual(d["GONE"]["scanner_status"], "INSUFFICIENT_DATA")
        self.assertTrue(d["OKONE"]["scanned_at"].endswith("IST"))
        # hourly labels carry the time of day, daily do not
        self.assertRegex(d["OKONE"]["scan_date_1h"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertRegex(d["OKONE"]["scan_date"], r"^\d{4}-\d\d-\d\d$")
        # derived 1H view exposes hourly values under plain names
        h = {r["symbol"]: r for r in json.loads(config.STOCKS_1H_JSON.read_text(encoding="utf-8"))}
        self.assertEqual(h["OKONE"]["scan_date"], d["OKONE"]["scan_date_1h"])
        self.assertEqual(h["OKONE"]["timeframe"], "1H")

    def test_failure_keeps_previous_good_result(self):
        first = self.run_scan(self.hist())
        before = {k: first["OKONE"][k] for k in ("trend", "signal", "top_signal_price", "scan_date")}
        # Yahoo now errors for OKONE (network) and returns nothing for OKTWO (delisted)
        d = self.run_scan(self.hist({"OKONE.NS": ConnectionError("boom"), "OKTWO.NS": "empty"}))
        for tf_s in ("", "_1h"):
            self.assertEqual(d["OKONE"][f"scanner_status{tf_s}"], "STALE")
            self.assertEqual(d["OKTWO"][f"scanner_status{tf_s}"], "STALE")
        self.assertEqual({k: d["OKONE"][k] for k in before}, before)          # analysis untouched
        self.assertIn("boom", d["OKONE"]["scan_error"])

    def test_error_without_history_is_error_state(self):
        d = self.run_scan(self.hist({"GONE.NS": ConnectionError("net down")}))
        self.assertEqual(d["GONE"]["scanner_status"], "ERROR")

    def test_concurrent_edit_is_not_clobbered(self):
        """Another script changes stocks.json while the scan is running - its change must survive."""
        mapping = self.hist()
        base = fake_history(mapping)
        calls = {"n": 0}

        def hist(t, i, s):
            calls["n"] += 1
            if calls["n"] == 3:
                common.modify_stocks(lambda st: st[0].__setitem__("sector", "EDITED-MEANWHILE"))
            return base(t, i, s)
        stocks = common.load_stocks()
        scan.run_scan(stocks, lambda s: ["1d"], workers=1, history_fn=hist, progress_every=1000)
        self.assertEqual(helpers.read()[0]["sector"], "EDITED-MEANWHILE")
        self.assertEqual(helpers.read()[0]["scanner_status"], "OK")

    def test_cli_smart_skip(self):
        orig = market_data._history
        market_data._history = fake_history(self.hist())
        try:
            self.assertEqual(scan.main(["--workers", "2"]), 0)
            st = helpers.read()
            self.assertEqual(st[0]["scanner_status"], "OK")
            stamp = st[0]["scanned_at"]
            market_data._history = fake_history({})                 # any download would now fail loudly
            self.assertEqual(scan.main(["--timeframes", "1d", "--workers", "2"]), 0)
            self.assertEqual(helpers.read()[0]["scanned_at"], stamp)   # daily skipped, nothing re-downloaded
        finally:
            market_data._history = orig


class Prices(unittest.TestCase):
    def test_refresh(self):
        helpers.seed_file(["AAA", "BBB", "CCC"])
        idx = pd.date_range("2026-09-14", periods=3)
        def frame(v): return pd.DataFrame({"Open": v, "High": v, "Low": v, "Close": v}, index=idx)
        def dl(tickers):
            parts = {t: frame(float(i + 10)) for i, t in enumerate(tickers) if t != "BBB.NS"}
            return pd.concat(parts, axis=1) if parts else pd.DataFrame()
        self.assertEqual(refresh.main([], download=dl), 0)
        d = {s["symbol"]: s for s in helpers.read()}
        self.assertEqual(d["AAA"]["current_price"], "10.0"); self.assertEqual(d["AAA"]["price_status"], "OK")
        self.assertEqual(d["BBB"]["price_status"], "NO_DATA"); self.assertEqual(d["BBB"]["current_price"], "NA")


class Enrich(unittest.TestCase):
    def test_no_overwrite_with_empty(self):
        recs = helpers.seed_file(["AAA"])
        recs[0].update(market_cap_cr=1234.5, sector="Tech", enrich_status="OK")
        common.save_stocks(recs)
        new = enrich.enrich_one(recs[0], info_fn=lambda t: {})
        self.assertEqual(new["enrich_status"], "NO_INFO")
        s = helpers.read()[0]
        enrich.merge(s, new, common.fmt_ts())
        self.assertEqual((s["market_cap_cr"], s["sector"]), (1234.5, "Tech"))     # kept

    def test_info_mapping_and_rank_caps(self):
        infos = {}
        stocks = []
        for i in range(300):
            s = {"symbol": f"S{i}", "instrument_group": "EQUITY", "market_cap_cr": 1000.0 + (300 - i)}
            stocks.append(s)
        stocks.append({"symbol": "SM1", "instrument_group": "SME", "market_cap_cr": 50})
        stocks.append({"symbol": "NOCAP", "instrument_group": "EQUITY", "market_cap_cr": None})
        enrich.classify_caps(stocks, "rank")
        caps = [s["cap_category"] for s in stocks]
        self.assertEqual(caps[:100], ["LARGE_CAP"] * 100)
        self.assertEqual(caps[100:250], ["MID_CAP"] * 150)
        self.assertEqual(caps[250], "SMALL_CAP")
        self.assertEqual((stocks[-2]["cap_category"], stocks[-1]["cap_category"]), ("SME", "NA"))
        got = enrich.enrich_one({"symbol": "XYZ", "instrument_group": "EQUITY"},
                                info_fn=lambda t: {"marketCap": 5e11, "sector": "Energy", "trailingPE": float("inf"),
                                                   "averageVolume": 1000})
        self.assertEqual(got["market_cap_cr"], 50000.0); self.assertNotIn("pe_ratio", got)   # inf dropped
        self.assertEqual(enrich.enrich_one({"symbol": "^NSEI", "instrument_group": "INDEX"})["enrich_status"], "SKIPPED")

    def test_needs_enrichment(self):
        now = common.now_ist()
        fresh = {"enrich_status": "OK", "enriched_at": common.fmt_ts(now - timedelta(days=1))}
        old = {"enrich_status": "OK", "enriched_at": common.fmt_ts(now - timedelta(days=9))}
        failed = {"enrich_status": "NO_INFO", "enriched_at": common.fmt_ts(now - timedelta(days=3))}
        self.assertFalse(enrich.needs_enrichment(fresh, False)); self.assertTrue(enrich.needs_enrichment(old, False))
        self.assertTrue(enrich.needs_enrichment(failed, False)); self.assertTrue(enrich.needs_enrichment(fresh, True))
        legacy = {"enriched_date": now.strftime("%Y-%m-%d"), "market_cap_cr": 10}
        self.assertFalse(enrich.needs_enrichment(legacy, False))

    def test_memberships(self):
        m = enrich.load_index_map()
        self.assertIn("Nifty 50", enrich.memberships("RELIANCE", m))


EQ_CSV = "SYMBOL,NAME OF COMPANY, SERIES,DATE OF LISTING\n" + "\n".join(
    f"EQ{i},Company {i} Ltd,EQ,01-JAN-2010" for i in range(1600)) + "\nBEONE,Be One Ltd,BE,01-JAN-2010\n"
SME_CSV = "SYMBOL,NAME OF COMPANY,SERIES\n" + "\n".join(f"SM{i},Sme {i},SM" for i in range(120)) + "\n"


class Universe(unittest.TestCase):
    def test_parse_and_plan(self):
        rows = uni.parse_master(EQ_CSV, "EQUITY")
        self.assertEqual(len(rows), 1600)                                   # BE series excluded
        recs = helpers.seed_file(["EQ1", "EQ2", "OLDGONE"])
        fetch = lambda url: SME_CSV if "SME" in url else EQ_CSV if "EQUITY_L" in url else (_ for _ in ()).throw(OSError("blocked"))
        lists = uni.collect(fetch)
        self.assertIsNone(lists["ETF"]); self.assertEqual(len(lists["SME"]), 120)
        plan = uni.plan_changes(common.load_stocks(), lists)
        self.assertIn("OLDGONE", plan["unlist"])
        self.assertNotIn("EQ1", plan["unlist"])
        self.assertTrue(any(c == "SME" for c, _ in plan["add"]))
        self.assertTrue(any(c == "INDEX" for c, _ in plan["add"]))
        uni.main(["--dry-run"], fetch=fetch)                                # dry run must not modify
        self.assertEqual(len(helpers.read()), 3)
        uni.main([], fetch=fetch)
        d = {s["symbol"]: s for s in helpers.read()}
        self.assertIs(d["OLDGONE"]["listed"], False)
        self.assertIn("EQ5", d); self.assertEqual(d["EQ5"]["scanner_status"], "PENDING_SCAN")

    def test_failed_download_flags_nothing(self):
        helpers.seed_file(["AAA"])
        lists = uni.collect(lambda url: (_ for _ in ()).throw(OSError("offline")))
        plan = uni.plan_changes(common.load_stocks(), lists)
        self.assertEqual(plan["unlist"], [])                                # offline must never mark everything unlisted


if __name__ == "__main__":
    unittest.main()
