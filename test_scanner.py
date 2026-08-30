import unittest
from datetime import datetime, timezone

import scanner


class ScannerLogicTests(unittest.TestCase):
    def test_negative_24h_change_cannot_be_acceleration(self):
        metrics = {
            "price_last_60m_pct": 1.0,
            "upper_wick_1h_pct": 0.2,
            "vol_15m_persistence_x": 2.0,
            "recent_15m_positive_count": 4,
            "four_hour_low_rising": True,
        }
        stage = scanner.classify_stage(-1.0, metrics, [], {"price_change_per_5m_pct": 1.0})
        self.assertNotEqual(stage, "acceleration")

    def test_fast_metrics_are_normalized_to_five_minutes(self):
        previous = {
            "generated_at_utc": "2026-08-30T00:00:00+00:00",
            "tickers": {"TEST": {"price": 100, "change24_pct": 1, "trade24_krw": 1_000_000_000}},
        }
        current = {
            "generated_at_utc": "2026-08-30T00:10:00+00:00",
            "tickers": {"TEST": {"price": 102, "change24_pct": 2, "trade24_krw": 1_200_000_000}},
        }
        result = scanner.fast_market_metrics("TEST", current, previous)
        self.assertEqual(result["scan_interval_minutes"], 10.0)
        self.assertEqual(result["price_change_per_5m_pct"], 1.0)
        self.assertEqual(result["trade_value_delta_per_5m_krw"], 100_000_000.0)

    def test_feature_pattern_similarity_does_not_need_ticker_identity(self):
        metrics = {
            "four_hour_low_rising": True,
            "recent_15m_positive_count": 3,
            "vol_15m_persistence_x": 1.2,
            "price_last_60m_pct": 1.0,
            "upper_wick_1h_pct": 0.3,
            "vol_1h_vs_20h_x": 1.3,
            "turnover_1h_vs_20h_x": 1.1,
        }
        self.assertEqual(scanner.success_pattern_similarity(metrics, 3.0), 100.0)

    def test_signal_requires_three_consecutive_usable_runs(self):
        row = {
            "momentum_stage": "pre_ignition",
            "failure_similarity_score": 20,
            "four_hour_low_rising": True,
            "price_last_60m_pct": 0.4,
        }
        self.assertEqual(scanner.consecutive_signal_runs([row, row], row), 3)
        broken = dict(row, price_last_60m_pct=-0.5)
        self.assertEqual(scanner.consecutive_signal_runs([row, broken], row), 1)

    def test_scorecard_uses_exact_independent_windows(self):
        start = datetime(2026, 8, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(20):
            price = 101 + index
            bars.append({
                "ts": int(start.timestamp() * 1000) + index * 15 * 60_000,
                "open": price - 1,
                "high": price + 0.5,
                "low": price - 1.5,
                "close": price,
            })
        history = [{
            "base": "TEST",
            "market": "KRW-TEST",
            "signal_type": "probe_buy",
            "recommended_at_utc": start.isoformat(),
            "entry_price": 100,
            "candle_entry_price": 100,
            "policy_version": scanner.SUPPORTED_POLICY_VERSION,
            "closed": False,
            "checkpoints": {},
        }]
        generated = datetime(2026, 8, 30, 4, 10, tzinfo=timezone.utc).isoformat()
        result = scanner.update_scorecard(
            history,
            {"KRW-TEST": {"trade_price": 120}},
            {"TEST": {"15m": bars}},
            generated,
        )[0]
        self.assertEqual(set(result["checkpoints"]), {"15", "60", "240"})
        self.assertEqual(result["checkpoints"]["15"]["return_pct"], 1.0)
        self.assertEqual(result["checkpoints"]["60"]["return_pct"], 4.0)
        self.assertEqual(result["checkpoints"]["240"]["return_pct"], 16.0)
        self.assertEqual(result["checkpoints"]["240"]["quality"], "exact_candle_window")

    def test_public_output_rejects_private_portfolio_fields(self):
        with self.assertRaises(RuntimeError):
            scanner.assert_public_output_safe({"result": {"평균매수가": 10}})
        scanner.assert_public_output_safe({"result": {"market_price": 10}})


if __name__ == "__main__":
    unittest.main()
