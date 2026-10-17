import unittest
from meteor_scatter.core.noise_watch import NoiseWatch


class NoiseWatchTests(unittest.TestCase):
    def test_warns_once_after_an_hour_then_once_on_recovery(self):
        w = NoiseWatch(calibrated_dbfs=-60.0)
        msgs = [w.observe(t, -45.0, 10.0 + t / 3600) for t in range(0, 3601, 30)]
        self.assertEqual(sum(m is not None for m in msgs), 1)
        self.assertIn("15.0 dB", msgs[-1])
        self.assertIn("front-end gain", msgs[-1])
        self.assertTrue(w.state()["alarmed"])
        more = [w.observe(3601 + t, -45.0, 11.0) for t in range(0, 600, 30)]
        self.assertTrue(all(m is None for m in more))
        back = w.observe(5000, -61.0, 11.0)
        self.assertIsNotNone(back)
        self.assertFalse(w.state()["alarmed"])

    def test_a_short_excursion_never_warns(self):
        w = NoiseWatch(calibrated_dbfs=-60.0)
        msgs = [w.observe(t, -45.0, 10.0) for t in range(0, 1800, 30)]
        msgs.append(w.observe(1800, -60.0, 10.0))
        msgs += [w.observe(1830 + t, -45.0, 10.0) for t in range(0, 1800, 30)]
        self.assertTrue(all(m is None for m in msgs))

    def test_uncalibrated_never_warns(self):
        w = NoiseWatch(calibrated_dbfs=None)
        self.assertIsNone(w.observe(0, -10.0, 10.0))
        self.assertIsNone(w.observe(7200, -10.0, 10.0))
