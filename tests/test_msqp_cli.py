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


class PackageHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Archive(self.root / "archive", 90, 2)
        _fill(self.a, self.root)
        self.day13 = self.root / "archive" / "6m" / "20261213"
        self.out = self.root / "msqp"

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, first=date(2026, 12, 13), last=date(2026, 12, 14)):
        return build_package(self.a, CFG, "AC0G", "EM38ww", first, last, self.out, "v1")

    def test_corrupt_sidecar_skipped_and_reported(self):
        (self.day13 / "261213_235930.json").write_text("{not json")
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertEqual(len(r.skipped), 1)
        self.assertIn("261213_235930.wav", r.skipped[0])

    def test_sidecar_missing_a_key_skipped(self):
        side = self.day13 / "261213_235930.json"
        d = json.loads(side.read_text()); del d["decodes"]; side.write_text(json.dumps(d))
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertIn("decodes", r.skipped[0])

    def test_missing_sidecar_skipped(self):
        (self.day13 / "261213_235930.json").unlink()
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertIn("missing", r.skipped[0])

    def test_symlinked_wav_outside_archive_skipped_and_never_zipped(self):
        secret = self.root / "secret.bin"
        secret.write_bytes(b"TOPSECRET" * 10)
        wav = self.day13 / "261213_235930.wav"
        wav.unlink(); wav.symlink_to(secret)
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertRegex(r.skipped[0], "symlink|outside")
        with zipfile.ZipFile(r.zip_path) as z:
            self.assertTrue(all(b"TOPSECRET" not in z.read(n) for n in z.namelist()))

    def test_all_invalid_refuses_and_writes_nothing(self):
        for j in self.root.glob("archive/6m/*/*.json"):
            j.write_text("[]")
        with self.assertRaises(PackageRefused):
            self._build()
        self.assertFalse(self.out.exists())

    def test_good_zip_survives_a_failed_rerun(self):
        r = self._build()
        good = r.zip_path.read_bytes()
        from unittest import mock
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._build()
        self.assertEqual(r.zip_path.read_bytes(), good)
        self.assertEqual([p.name for p in r.dir.iterdir() if p.name.startswith(".")], [])

    def test_swap_between_passes_never_zips_the_outside_file(self):
        from unittest import mock
        from meteor_scatter import msqp_cli
        secret = self.root / "secret.bin"
        secret.write_bytes(b"TOPSECRET" * 10)
        wav = self.day13 / "261213_235930.wav"
        real, seen = msqp_cli._open_regular, []

        def swapping(path, *a, **kw):
            if Path(path).name == wav.name:
                seen.append(1)
                if len(seen) == 2:                  # the zip pass
                    wav.unlink(); wav.symlink_to(secret)
            return real(path, *a, **kw)
        with mock.patch.object(msqp_cli, "_open_regular", swapping):
            with self.assertRaises(PackageRefused):
                self._build()
        for z in self.out.rglob("*.zip"):
            self.fail("a zip was left behind")
        self.assertEqual([p for p in self.out.rglob("*") if p.is_file()], [])

    def test_day_directory_swapped_at_open_never_reaches_the_zip(self):
        import os
        from unittest import mock
        from meteor_scatter import msqp_cli
        outside = self.root / "outside"; outside.mkdir()
        (outside / "261213_235930.wav").write_bytes(b"TOPSECRET" * 10)
        moved = self.root / "day13-moved"
        real = msqp_cli._open_regular

        def swapping(name, *a, **kw):
            # the attacker shows a symlinked day while the wav opens, the real day otherwise
            want_link = Path(name).name == "261213_235930.wav"
            is_link = self.day13.is_symlink()
            if want_link and not is_link:
                os.rename(self.day13, moved); self.day13.symlink_to(outside)
            elif not want_link and is_link:
                self.day13.unlink(); os.rename(moved, self.day13)
            return real(name, *a, **kw)
        with mock.patch.object(msqp_cli, "_open_regular", swapping):
            try:
                r = self._build()
            except PackageRefused:
                return
        with zipfile.ZipFile(r.zip_path) as z:
            self.assertTrue(all(b"TOPSECRET" not in z.read(n) for n in z.namelist()))

    def test_wav_replaced_by_another_regular_file_between_passes_refused(self):
        import os
        from unittest import mock
        from meteor_scatter import msqp_cli
        wav = self.day13 / "261213_235930.wav"
        real, seen = msqp_cli._open_regular, []

        def swapping(name, *a, **kw):
            if Path(name).name == wav.name:
                seen.append(1)
                if len(seen) == 2:                  # the zip pass
                    other = self.day13 / "new.tmp"
                    other.write_bytes(b"IMPOSTOR" * 10)
                    os.rename(other, wav)           # new inode, still a regular file
            return real(name, *a, **kw)
        with mock.patch.object(msqp_cli, "_open_regular", swapping):
            with self.assertRaisesRegex(PackageRefused, "changed during packaging"):
                self._build()
        self.assertEqual([p for p in self.out.rglob("*") if p.is_file()], [])

    def test_every_line_separator_in_a_message_is_skipped(self):
        side = self.day13 / "261213_235930.json"
        base = json.loads(side.read_text())
        for ch in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            d = json.loads(json.dumps(base)); d["decodes"][0]["message"] = f"CQ{ch}X"
            side.write_text(json.dumps(d))
            r = self._build()
            self.assertEqual(r.files, 1, repr(ch))
            self.assertIn("line break", r.skipped[0], repr(ch))

    def test_hardlinked_wav_skipped(self):
        import os
        os.link(self.day13 / "261213_235930.wav", self.root / "other.wav")
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertIn("hard links", r.skipped[0])

    def test_decode_message_with_line_break_skipped(self):
        side = self.day13 / "261213_235930.json"
        d = json.loads(side.read_text()); d["decodes"][0]["message"] = "CQ X\n20261213 forged"
        side.write_text(json.dumps(d))
        r = self._build()
        self.assertEqual(r.files, 1)
        self.assertIn("line break", r.skipped[0])
        self.assertNotIn("forged", (r.dir / "ALL.TXT").read_text())

    def test_output_root_group_writable_or_foreign_refused(self):
        import os
        from unittest import mock
        self.out.mkdir(); os.chmod(self.out, 0o775)
        with self.assertRaisesRegex(PackageRefused, "writable"):
            self._build()
        os.chmod(self.out, 0o755)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(PackageRefused, "owned"):
                self._build()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_package_dir_swapped_after_open_does_not_redirect_writes(self):
        import os
        from unittest import mock
        from meteor_scatter import msqp_cli
        target = self.root / "elsewhere"; target.mkdir()
        name = "AC0G EM38WW 20261213-14 Meteor Scatter QSO Party"
        real, calls = msqp_cli._open_dir, []

        def swapping(path, what, dir_fd=None):
            fd = real(path, what, dir_fd)
            calls.append(path)
            if path == name:
                os.rename(self.out / name, self.out / "moved")
                (self.out / name).symlink_to(target)
            return fd
        with mock.patch.object(msqp_cli, "_open_dir", swapping):
            self._build()
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue((self.out / "moved" / "ALL.TXT").exists())
        self.assertTrue((self.out / "moved" / f"{name}.zip").exists())

    def test_symlinked_package_dir_refused(self):
        self.out.mkdir(); self.out.chmod(0o755)
        target = self.root / "elsewhere"; target.mkdir()
        (self.out / "AC0G EM38WW 20261213-14 Meteor Scatter QSO Party").symlink_to(target)
        with self.assertRaisesRegex(PackageRefused, "symlink"):
            self._build()
        self.assertEqual(list(target.iterdir()), [])


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

    def test_out_through_a_symlink_refused(self):
        real = self.root / "real-out"; real.mkdir()
        link = self.root / "link-out"; link.symlink_to(real)
        args = argparse.Namespace(instance="AC0G-B4", config=self.cfg, date_from="2026-12-13",
                                  date_to="2026-12-14", out=str(link), log_level=None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cmd_package(args)
        self.assertEqual(rc, 2)
        self.assertIn("symlink", err.getvalue())
        self.assertEqual(list(real.iterdir()), [])

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

    def test_several_radiod_blocks_without_reporter_id_need_a_matching_instance(self):
        text = PKG_CONFIG.format(archive=self.archive_dir).replace(
            '[instance]\nreporter_id = "AC0G/B4"\n\n', "")
        text += '\n[[radiod]]\nstatus = "other.local"\n'
        self.cfg.write_text(text)
        rc, out, err = self._run()                  # instance "AC0G-B4" names neither block
        self.assertEqual(rc, 2)
        self.assertIn("several [[radiod]] blocks", err)
        self.assertIn("rx.local", err)
