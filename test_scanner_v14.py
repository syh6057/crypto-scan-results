import unittest

import scanner_v14 as v14


class ScannerV14LogicTests(unittest.TestCase):
    def test_low_turnover_early_mover_is_visible_to_radar(self):
        fast = {
            "price_change_per_5m_pct": 0.35,
            "change24_delta_since_scan_pct": 0.5,
            "trade_value_delta_per_5m_krw": 15_000_000,
        }
        self.assertTrue(v14._is_radar_candidate(4.0, 50_000_000, fast))

    def test_warning_does_not_hide_detection(self):
        current = {
            "generated_at_utc": "2026-09-05T15:00:00+00:00",
            "tickers": {
                "RISK": {"market": "KRW-RISK", "price": 100, "change24_pct": 6, "trade24_krw": 100_000_000},
            },
        }
        previous = {
            "generated_at_utc": "2026-09-05T14:55:00+00:00",
            "tickers": {
                "RISK": {"market": "KRW-RISK", "price": 99, "change24_pct": 5, "trade24_krw": 80_000_000},
            },
        }
        markets = {"RISK": {"market": "KRW-RISK", "market_warning": "CAUTION"}}
        radar = v14._build_full_market_radar(current, previous, markets, {})
        self.assertEqual(radar[0]["base"], "RISK")
        self.assertTrue(radar[0]["high_risk_market"])
        self.assertIn("VISIBLE", radar[0]["detection_status"])

    def test_rank_churn_does_not_invalidate_intact_structure(self):
        previous = {
            "fast_breakout_leader": {"base": "KEEP"},
        }
        current = {
            "snapshot": {
                "KEEP": {
                    "base": "KEEP",
                    "momentum_stage": "pre_ignition",
                    "failure_similarity_score": 20,
                    "four_hour_low_rising": True,
                    "price_change_per_5m_pct": 0.0,
                    "future_expansion_score": 50,
                    "bithumb_krw_price": 10,
                }
            },
            "pre_ignition_top5": [],
            "acceleration_top5": [],
            "fast_breakout_alerts": [],
        }
        continuity = v14._build_continuity_signals(previous, current)
        self.assertEqual(continuity[0]["base"], "KEEP")
        self.assertTrue(continuity[0]["rank_churn_only"])
        self.assertTrue(continuity[0]["still_valid"])

    def test_additional_rows_rank_breakout_before_plain_turnover(self):
        current = {
            "generated_at_utc": "2026-09-05T15:00:00+00:00",
            "tickers": {
                "FAST": {"price": 101, "change24_pct": 3, "trade24_krw": 50_000_000},
                "SLOW": {"price": 100, "change24_pct": 0.5, "trade24_krw": 500_000_000},
            },
        }
        previous = {
            "generated_at_utc": "2026-09-05T14:55:00+00:00",
            "tickers": {
                "FAST": {"price": 100, "change24_pct": 2, "trade24_krw": 40_000_000},
                "SLOW": {"price": 100, "change24_pct": 0.5, "trade24_krw": 490_000_000},
            },
        }
        rows = [
            {"base": "SLOW", "bithumb_24h_change_pct": 0.5, "bithumb_24h_trade_krw": 500_000_000},
            {"base": "FAST", "bithumb_24h_change_pct": 3, "bithumb_24h_trade_krw": 50_000_000},
        ]
        enriched = v14._enrich_additional(rows, current, previous)
        self.assertEqual(enriched[0]["base"], "FAST")
        self.assertTrue(enriched[0]["breakout_radar_candidate"])


if __name__ == "__main__":
    unittest.main()
