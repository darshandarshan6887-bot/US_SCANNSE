import os, sys, unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import smc_engine as eng


def reference(highs, lows, closes, size):
    """Deliberately naive, bar-by-bar, streaming port of the Pine logic (no vectorisation)."""
    n = len(closes)
    leg = 0
    trend, event, event_bar = "UNKNOWN", "NONE", -1
    hi = lo = None            # [level, bar, broken]
    top = bot = None
    top_bar = bot_bar = -1
    trace = []
    for i in range(n):
        new_leg = leg
        if i - size >= 0:
            p = i - size
            win_h = max(highs[p + 1:i + 1])
            win_l = min(lows[p + 1:i + 1])
            if highs[p] > win_h:
                new_leg = 0
            elif lows[p] < win_l:
                new_leg = 1
        if new_leg != leg:
            p = i - size
            if new_leg == 1:
                lo = [lows[p], p, False]; bot, bot_bar = lows[p], p
            else:
                hi = [highs[p], p, False]; top, top_bar = highs[p], p
        leg = new_leg
        if i > 0:
            if hi and not hi[2] and closes[i - 1] <= hi[0] < closes[i]:
                event = "CHOCH" if trend == "BEARISH" else "BOS"
                trend, hi[2], event_bar = "BULLISH", True, i
            if lo and not lo[2] and closes[i - 1] >= lo[0] > closes[i]:
                event = "CHOCH" if trend == "BULLISH" else "BOS"
                trend, lo[2], event_bar = "BEARISH", True, i
        if top is not None and highs[i] >= top:
            top, top_bar = highs[i], i
        if bot is not None and lows[i] <= bot:
            bot, bot_bar = lows[i], i
        trace.append((trend, event, event_bar, top, top_bar, bot, bot_bar))
    return trace


def series(seed, n=400, vol=0.012, grid=None):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    h = c * (1 + np.abs(rng.normal(0, 0.005, n)))
    l = c * (1 - np.abs(rng.normal(0, 0.005, n)))
    if grid:                                    # coarse prices -> many equal highs/lows (ties)
        c, h, l = (np.round(x / grid) * grid for x in (c, h, l))
        h = np.maximum(h, np.maximum(c, l)); l = np.minimum(l, np.minimum(c, h))
    return h, l, c


class EngineMatchesReference(unittest.TestCase):
    def test_random_series_all_sizes(self):
        for seed in range(60):
            for size in (3, 5, 10, 25, 50):
                h, l, c = series(seed, n=300 + seed * 5, grid=[None, 0.5][seed % 2])
                a = eng.analyze(h, l, c, size)
                t = reference(h, l, c, size)[-1]
                self.assertEqual((a.trend, a.event, a.event_bar), t[:3], (seed, size))
                self.assertEqual((a.top_price, a.top_bar, a.bottom_price, a.bottom_bar), (t[3], t[4], t[5], t[6]), (seed, size))

    def test_no_lookahead(self):
        """Result on a prefix must equal the streaming state at that bar of the full run."""
        h, l, c = series(7, n=500)
        trace = reference(h, l, c, 10)
        for k in (60, 111, 200, 333, 499, 500):
            a = eng.analyze(h[:k], l[:k], c[:k], 10)
            self.assertEqual((a.trend, a.event, a.event_bar), trace[k - 1][:3], k)


class Scenarios(unittest.TestCase):
    def test_interleaved_breaks_use_time_order(self):
        """Low pivot broken (bearish) BEFORE the earlier high pivot is broken (bullish):
        the trend must end BULLISH because the last break in time was the upside one.
        (The old pivot-order algorithm ended BEARISH here.)"""
        size = 2
        # bars:   0   1   2   3   4   5   6   7   8   9  10  11  12  13  14
        highs = [ 5,  6,  7,  9,  8,  7,  6,  5,  6,  7, 8.5, 9.5, 10, 11, 12]
        lows  = [ 4,  5,  6,  8,  7,  6,  5,  3,  4,  5,  6,   7,  8,  9, 10]
        close = [ 4.5,5.5,6.5,8.5,7.5,6.5,5.5,3.5,4.5,5.5,6.5, 8, 9,10.5,11.5]
        a = eng.analyze(highs, lows, close, size)
        t = reference(highs, lows, close, size)[-1]
        self.assertEqual((a.trend, a.event, a.event_bar), t[:3])

    def test_fields_and_suffix(self):
        h, l, c = series(3, n=300)
        df = pd.DataFrame({"Date": pd.date_range("2025-01-01", periods=300, freq="B"),
                           "High": h, "Low": l, "Close": c})
        f = eng.analyze_frame(df, "", "1D", "%Y-%m-%d", min_bars=120, size=10)
        self.assertEqual(f["scanner_status"], "OK")
        if f["trend"] == "BULLISH":
            self.assertEqual((f["bottom_signal"], f["top_signal"]), ("STRONG_LOW", "WEAK_HIGH"))
        if f["trend"] == "BEARISH":
            self.assertEqual((f["top_signal"], f["bottom_signal"]), ("STRONG_HIGH", "WEAK_LOW"))
        self.assertEqual(f["scan_date"], df["Date"].iloc[-1].strftime("%Y-%m-%d"))
        g = eng.analyze_frame(df, "_1h", "1H", "%Y-%m-%d %H:%M", min_bars=120, size=10)
        self.assertTrue(all(k.endswith("_1h") for k in g))
        self.assertEqual({k[:-3] for k in g}, set(f))
        self.assertEqual(set(eng.empty_fields("", "1D", "X")), set(f))

    def test_insufficient_and_no_pivots(self):
        df = pd.DataFrame({"Date": pd.date_range("2025-01-01", periods=30), "High": 1.0, "Low": 1.0, "Close": 1.0})
        self.assertEqual(eng.analyze_frame(df, "", "1D", "%Y-%m-%d")["scanner_status"], "INSUFFICIENT_DATA")
        flat = pd.DataFrame({"Date": pd.date_range("2025-01-01", periods=200), "High": 5.0, "Low": 5.0, "Close": 5.0})
        self.assertEqual(eng.analyze_frame(flat, "", "1D", "%Y-%m-%d")["scanner_status"], "NO_PIVOTS")

    def test_rank_score(self):
        self.assertEqual(eng.rank_score("STRONG_LOW", 0, "CHOCH"), 110.0)
        self.assertEqual(eng.rank_score("WEAK_HIGH", 100, "NONE"), 15.0)
        self.assertEqual(eng.rank_score("NO_SIGNAL", -1, "BOS"), 0)


if __name__ == "__main__":
    unittest.main()
