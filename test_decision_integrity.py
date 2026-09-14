import unittest
import scanner_v38 as v38


def row(**overrides):
    base = {
        "base": "TEST",
        "price_krw": 100.0,
        "change_24h_pct": 2.0,
        "trade_24h_krw": 500_000_000,
        "winner_pattern": {"winner_edge": 0.13, "positive_similarity": 0.70},
        "metrics": {
            "m15_vol_persistence_x": 2.0,
            "buy_sell_ratio": 1.5,
            "bid_ask_depth_ratio": 1.2,
            "price_change_per_5m_pct": 0.1,
        },
    }
    for k, v in overrides.items():
        if k == "metrics":
            base["metrics"].update(v)
        elif k == "winner_pattern":
            base["winner_pattern"].update(v)
        else:
            base[k] = v
    return base


class DecisionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.market = {"price_krw": 100.2, "market_warning": "NONE"}

    def test_normal_candidate_passes_quality(self):
        ok, reasons = v38.data_quality(row(), self.market)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_buy_sell_99_is_blocked_as_sentinel(self):
        ok, reasons = v38.data_quality(row(metrics={"buy_sell_ratio": 99.0}), self.market)
        self.assertFalse(ok)
        self.assertIn("buy_sell_sentinel_or_zero_sell_sample", reasons)

    def test_flow_orderbook_contradiction_is_blocked(self):
        ok, reasons = v38.data_quality(row(metrics={"buy_sell_ratio": 7.0, "bid_ask_depth_ratio": 0.1}), self.market)
        self.assertFalse(ok)
        self.assertIn("flow_orderbook_contradiction_buy_vs_ask_wall", reasons)

    def test_stale_bridge_price_is_blocked(self):
        ok, reasons = v38.data_quality(row(price_krw=110.0), self.market)
        self.assertFalse(ok)
        self.assertIn("bridge_vs_market_price_divergence", reasons)

    def test_fast_track_requires_balanced_strong_live_flow(self):
        self.assertTrue(v38.fast_track(row()))
        self.assertFalse(v38.fast_track(row(metrics={"bid_ask_depth_ratio": 0.1})))
        self.assertFalse(v38.fast_track(row(winner_pattern={"winner_edge": 0.07})))


if __name__ == "__main__":
    unittest.main()
