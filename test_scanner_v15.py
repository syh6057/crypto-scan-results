import unittest

import scanner_v15 as v15


class ScannerV15LogicTests(unittest.TestCase):
    def test_gap_jump_over_25_remains_visible(self):
        fast = {
            "price_change_per_5m_pct": 1.2,
            "change24_delta_since_scan_pct": 6.0,
            "trade_value_delta_per_5m_krw": 80_000_000,
        }
        self.assertTrue(v15._is_gap_jump(32.0, 1_000_000_000, fast))
        self.assertTrue(v15._is_radar_candidate(32.0, 1_000_000_000, fast))

    def test_stale_overheated_mover_is_not_reintroduced(self):
        fast = {
            "price_change_per_5m_pct": 0.05,
            "change24_delta_since_scan_pct": 0.1,
            "trade_value_delta_per_5m_krw": 1_000_000,
        }
        self.assertFalse(v15._is_gap_jump(35.0, 1_000_000_000, fast))
        self.assertFalse(v15._is_radar_candidate(35.0, 1_000_000_000, fast))

    def test_gap_alert_is_explicitly_no_chase(self):
        current = {
            "generated_at_utc": "2026-09-06T04:20:00+00:00",
            "tickers": {
                "FAST": {
                    "market": "KRW-FAST",
                    "price": 130,
                    "change24_pct": 30,
                    "trade24_krw": 2_000_000_000,
                }
            },
        }
        previous = {
            "generated_at_utc": "2026-09-06T04:15:00+00:00",
            "tickers": {
                "FAST": {
                    "market": "KRW-FAST",
                    "price": 120,
                    "change24_pct": 20,
                    "trade24_krw": 1_500_000_000,
                }
            },
        }
        alerts = v15._build_gap_breakout_alerts(current, previous, {"FAST": {"market": "KRW-FAST", "market_warning": "NONE"}})
        self.assertEqual(alerts[0]["base"], "FAST")
        self.assertFalse(alerts[0]["chase_allowed"])
        self.assertEqual(alerts[0]["detection_status"], "GAP_BREAKOUT_VISIBLE_NO_CHASE")


if __name__ == "__main__":
    unittest.main()
