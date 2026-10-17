import argparse
import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from meteor_scatter.msqp_cli import calibrate_scale, cmd_calibrate


class CalibrateTests(unittest.TestCase):
    def test_puts_noise_fifty_db_below_full_scale(self):
        scale, cal = calibrate_scale([-82.0, -81.0, -80.0, -95.0, -60.0, -81.5, -80.5, -81.0, -82.0, -80.0])
        self.assertAlmostEqual(cal, -81.0, places=1)        # median, not mean
        self.assertAlmostEqual(scale, 31.0, places=1)       # -50 - (-81)

    def test_refuses_too_few_slots(self):
        with self.assertRaises(ValueError):
            calibrate_scale([-80.0] * 9)


CONFIG = """\
[station]
callsign = "AC0G"
grid = "EM38"

[paths]
spool_dir = "{spool}"

[[radiod]]
status = "rx.local"

[radiod.msk144]
freqs_hz = [28145000, 50260000]
"""


class CmdCalibrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "spool"
        (self.spool / "rx.local").mkdir(parents=True)
        self.cfg = self.root / "ms.toml"
        self.cfg.write_text(CONFIG.format(spool=self.spool))

    def tearDown(self):
        self.tmp.cleanup()

    def _noise(self, freq, floors):
        lines = [f"{1797206370 + 30 * i} {f}" for i, f in enumerate(floors)]
        (self.spool / "rx.local" / f"noise-{freq}.txt").write_text("\n".join(lines) + "\n")

    def _run(self, dry_run=False):
        args = argparse.Namespace(instance="AC0G-B4", config=self.cfg, dry_run=dry_run,
                                  log_level=None)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_calibrate(args)
        return rc, out.getvalue(), err.getvalue()

    def test_writes_both_tables_and_skips_a_thin_band(self):
        self._noise(28145000, [-81.0] * 12)
        self._noise(50260000, [-70.0] * 4)          # too few
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertIn("10m: noise -81.0 dBFS → scale_db 31.0", out)
        self.assertIn("6m: ", out)
        self.assertIn("need 10 slots", out)
        self.assertIn("restart meteor-scatter@AC0G-B4 to apply; this restarts no radiod", out)
        got = tomllib.loads(self.cfg.read_text())
        m = got["radiod"][0]["msk144"]
        self.assertEqual(m["scale_db"], {"28145000": 31.0})
        self.assertEqual(m["noise_cal_dbfs"], {"28145000": -81.0})
        self.assertEqual(m["freqs_hz"], [28145000, 50260000])
        self.assertEqual(got["station"]["callsign"], "AC0G")

    def test_dry_run_writes_nothing(self):
        self._noise(28145000, [-81.0] * 12)
        before = self.cfg.read_text()
        rc, out, _ = self._run(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("scale_db 31.0", out)
        self.assertEqual(self.cfg.read_text(), before)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_exit_two_when_no_band_can_be_calibrated(self):
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertNotIn("restart meteor-scatter", out)
        self.assertNotIn("scale_db", self.cfg.read_text())

    def test_keeps_other_bands_existing_values(self):
        self.cfg.write_text(self.cfg.read_text() +
                            '\n[radiod.msk144.scale_db]\n"50260000" = 12.5\n')
        self._noise(28145000, [-81.0] * 12)
        rc, _, err = self._run()
        self.assertEqual(rc, 0, err)
        m = tomllib.loads(self.cfg.read_text())["radiod"][0]["msk144"]
        self.assertEqual(m["scale_db"], {"28145000": 31.0, "50260000": 12.5})
