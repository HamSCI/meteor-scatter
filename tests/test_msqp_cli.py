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


import json, zipfile
from datetime import date, datetime, timezone
from meteor_scatter.core.archive import Archive, instance_dirname
from meteor_scatter.msqp_cli import build_package, cmd_package, PackageRefused

CFG = {"operator": "Michael Hauan AC0G", "contact_email": "a@b.c", "antenna": "dipole", "comments": ""}


def _fill(archive, tmp):
    for t, clip in ((1797206370.0, 0), (1797206430.0, 4)):     # 12-13 23:59:30, 12-14 00:00:30
        w = tmp / "w.wav"; w.write_bytes(b"\0" * 64)
        iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
        archive.keep(w, 50260000, t, {"slot_start_utc": iso, "dial_freq_hz": 50260000,
            "clipped_samples": clip, "frontend": {"overloaded": False},
            "decodes": [{"snr": -2, "dt": 0.3, "audio_freq_hz": 1500, "message": "CQ X"}]})


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Archive(self.root / "archive", 90, 2)
        _fill(self.a, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_zip_alltxt_description_and_metadata(self):
        r = build_package(self.a, CFG, "AC0G/B4", "EM38ww", date(2026, 12, 13), date(2026, 12, 14),
                          self.root / "msqp", "v1")
        self.assertEqual(r.dir.name, "AC0G-B4 EM38WW 20261213-14 Meteor Scatter QSO Party")
        with zipfile.ZipFile(r.zip_path) as z:
            self.assertEqual(sorted(z.namelist()), [
                "6m/20261213/261213_235930.json", "6m/20261213/261213_235930.wav",
                "6m/20261214/261214_000030.json", "6m/20261214/261214_000030.wav"])
        self.assertEqual(len((r.dir / "ALL.TXT").read_text().splitlines()), 2)
        self.assertTrue((r.dir / "description.md").exists())
        self.assertIn("title", json.loads((r.dir / "metadata.json").read_text())["metadata"])
        self.assertEqual(r.flagged, ["6m/20261214/261214_000030.wav"])

    def test_range_by_start_day(self):
        r = build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 12, 13), date(2026, 12, 13),
                          self.root / "msqp", "v1")
        self.assertEqual(r.files, 1)

    def test_refuses_reversed_range_and_writes_nothing(self):
        with self.assertRaises(PackageRefused):
            build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 12, 14), date(2026, 12, 13),
                          self.root / "msqp", "v1")
        self.assertFalse((self.root / "msqp").exists())

    def test_refuses_empty_range_and_writes_nothing(self):
        with self.assertRaisesRegex(PackageRefused, "no recordings"):
            build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 8, 1), date(2026, 8, 2),
                          self.root / "msqp", "v1")
        self.assertFalse((self.root / "msqp").exists())

    def test_refuses_missing_operator_field_by_name_and_writes_nothing(self):
        with self.assertRaisesRegex(PackageRefused, "contact_email"):
            build_package(self.a, dict(CFG, contact_email=""), "AC0G", "EM38ww",
                          date(2026, 12, 13), date(2026, 12, 14), self.root / "msqp", "v1")
        self.assertFalse((self.root / "msqp").exists())

    def test_rerun_replaces_files_but_keeps_draft_json(self):
        args = (self.a, CFG, "AC0G", "EM38ww", date(2026, 12, 13), date(2026, 12, 14),
                self.root / "msqp", "v1")
        r = build_package(*args)
        (r.dir / "draft.json").write_text('{"id": 7}')
        (r.dir / "ALL.TXT").write_text("stale")
        r2 = build_package(*args)
        self.assertEqual(r2.dir, r.dir)
        self.assertEqual((r.dir / "draft.json").read_text(), '{"id": 7}')
        self.assertEqual(len((r.dir / "ALL.TXT").read_text().splitlines()), 2)
        with zipfile.ZipFile(r2.zip_path) as z:
            self.assertEqual(len(z.namelist()), 4)


PKG_CONFIG = """\
[station]
callsign = "AC0G"
grid_square = "EM38ww40pk"

[instance]
reporter_id = "AC0G/B4"

[msqp]
archive_dir = "{archive}"
operator = "Michael Hauan AC0G"
contact_email = "a@b.c"
antenna = "dipole"

[[radiod]]
status = "rx.local"
"""


class CmdPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive_dir = self.root / "archive"
        self.cfg = self.root / "ms.toml"
        self.cfg.write_text(PKG_CONFIG.format(archive=self.archive_dir))
        # per-instance root, as the daemon lays it out
        self.a = Archive(self.archive_dir / instance_dirname("AC0G/B4"), 90, 2)
        _fill(self.a, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, first="2026-12-13", last="2026-12-14"):
        args = argparse.Namespace(instance="AC0G-B4", config=self.cfg, date_from=first,
                                  date_to=last, out=str(self.root / "out"), log_level=None)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_package(args)
        return rc, out.getvalue(), err.getvalue()

    def test_packages_from_the_per_instance_archive(self):
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        d = self.root / "out" / "AC0G-B4 EM38WW 20261213-14 Meteor Scatter QSO Party"
        self.assertTrue((d / f"{d.name}.zip").exists())
        md = json.loads((d / "metadata.json").read_text())["metadata"]
        self.assertTrue(md["title"].startswith("AC0G/B4 EM38WW"))
        self.assertIn("6m 20261213: 1", out)
        self.assertIn("2 recordings", out)
        self.assertIn("flagged", out)
        self.assertIn("261214_000030.wav", out)

    def test_bad_date_refuses_with_exit_2_and_writes_nothing(self):
        rc, out, err = self._run(first="2026-13-01")
        self.assertEqual(rc, 2)
        self.assertIn("--from", err)
        self.assertFalse((self.root / "out").exists())

    def test_empty_instance_archive_refuses(self):
        rc, out, err = self._run(first="2026-08-01", last="2026-08-02")
        self.assertEqual(rc, 2)
        self.assertIn("no recordings", err)
        self.assertFalse((self.root / "out").exists())
