import unittest
from meteor_scatter.core.frontend import FrontendLog, FrontendReading

SR = 12000


def r(t, gain=10.0, atten=0.0, over=0):
    return FrontendReading(t=t, rf_gain=gain, rf_atten=atten, rf_agc=True,
                           if_power=-23.5, ad_over=over)


class WindowTests(unittest.TestCase):
    def test_steady_gain_has_no_steps(self):
        log = FrontendLog()
        for i in range(0, 40):
            log.add(r(1000.0 + i * 0.5))
        w = log.window(1005.0, 1015.0, SR)
        self.assertEqual(w["frontend_status"], "ok")
        self.assertEqual(w["steps"], [])
        self.assertEqual(w["start"]["rf_gain"], 10.0)

    def test_step_is_a_window_between_packets(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1004.5)); log.add(r(1005.0, gain=12.5)); log.add(r(1020.0, gain=12.5))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(len(w["steps"]), 1)
        s = w["steps"][0]
        self.assertEqual((s["from_db"], s["to_db"]), (10.0, 12.5))
        self.assertEqual(s["sample_window"], [int(4.5 * SR), int(5.0 * SR)])

    def test_gap_over_five_seconds_marks_missing(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1012.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "partial")
        self.assertIn([0, 12 * SR], w["missing_sample_windows"])

    def test_no_readings_at_all_is_missing(self):
        w = FrontendLog().window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "missing")
        self.assertIsNone(w["start"])

    def test_ad_over_rise_marks_overload(self):
        log = FrontendLog()
        log.add(r(999.0, over=7)); log.add(r(1010.0, over=7)); log.add(r(1012.0, over=9))
        w = log.window(1000.0, 1015.0, SR)
        self.assertEqual(w["ad_over_delta"], 2)
        self.assertTrue(w["overloaded"])

    def test_log_keeps_only_changes_or_one_per_second(self):
        log = FrontendLog()
        for i in range(100):
            log.add(r(1000.0 + i * 0.01))        # 100 packets in one second, no change
        self.assertLessEqual(len(log._readings), 2)

    def test_only_stale_reading_before_window_is_missing(self):
        log = FrontendLog()
        log.add(r(500.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "missing")
        self.assertEqual(w["missing_sample_windows"], [[0, 30 * SR]])

    def test_ad_over_counter_reset_is_unknown_not_false(self):
        log = FrontendLog()
        log.add(r(999.0, over=500)); log.add(r(1005.0, over=500)); log.add(r(1010.0, over=3))
        w = log.window(1000.0, 1015.0, SR)
        self.assertIsNone(w["ad_over_delta"])
        self.assertIsNone(w["overloaded"])
        self.assertTrue(w["ad_over_reset"])

    def test_ad_over_reset_false_when_counter_rises(self):
        log = FrontendLog()
        log.add(r(999.0, over=7)); log.add(r(1010.0, over=9))
        self.assertFalse(log.window(1000.0, 1015.0, SR)["ad_over_reset"])
