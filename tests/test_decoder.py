"""Tests for meteor_scatter.core.decoder — jt9 MSK144 helpers.

Covers binary resolution, the jt9 argv, decoded.txt line parsing, and
the normalization that turns a jt9 decode into a per-mode-log line.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from meteor_scatter.core import decoder


class TestResolveBinary(unittest.TestCase):
    """resolve_jt9_binary takes the first candidate that decodes MSK144."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.wsjtx = self.tmp / "wsjtx-jt9"
        self.wsjtx.write_text("")
        self.override = self.tmp / "override-jt9"
        self.override.write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_override_wins_when_it_decodes_msk144(self):
        got = decoder.resolve_jt9_binary(str(self.override), probe=lambda p: True,
                                         wsjtx_path=str(self.wsjtx))
        self.assertEqual(got, str(self.override))

    def test_wsjtx_build_beats_the_path_symlink(self):
        got = decoder.resolve_jt9_binary("", probe=lambda p: True,
                                         wsjtx_path=str(self.wsjtx))
        self.assertEqual(got, str(self.wsjtx))

    def test_a_jt9_without_msk144_is_skipped(self):
        # The 2026-10-26 outage: the override (or the PATH symlink) points at
        # UberSDR's jt9, which has no --msk144.  The WSJT-X build still decodes.
        got = decoder.resolve_jt9_binary(str(self.override),
                                         probe=lambda p: p == str(self.wsjtx),
                                         wsjtx_path=str(self.wsjtx))
        self.assertEqual(got, str(self.wsjtx))

    def test_no_msk144_decoder_anywhere_raises(self):
        with self.assertRaises(decoder.NoMsk144Decoder) as cm:
            decoder.resolve_jt9_binary(str(self.override), probe=lambda p: False,
                                       wsjtx_path=str(self.wsjtx))
        self.assertIn(str(self.wsjtx), str(cm.exception))

    def test_missing_override_is_not_returned(self):
        got = decoder.resolve_jt9_binary("/no/such/jt9-binary", probe=lambda p: True,
                                         wsjtx_path=str(self.wsjtx))
        self.assertEqual(got, str(self.wsjtx))


class TestProbe(unittest.TestCase):

    def _run(self, out=b"", err=b"", exc=None):
        from types import SimpleNamespace
        def run(argv, **kw):
            if exc:
                raise exc
            return SimpleNamespace(returncode=0, stdout=out, stderr=err)
        return run

    def test_help_listing_msk144_passes(self):
        self.assertTrue(decoder.jt9_decodes_msk144(
            "jt9", run=self._run(b" --msk144\n    MSK144 mode\n")))

    def test_ubersdr_help_fails(self):
        # UberSDR's jt9 lists --ft2, --ft4, --ft8, --fst4w, and no --msk144.
        self.assertFalse(decoder.jt9_decodes_msk144(
            "jt9", run=self._run(b" --ft2\n --ft4\n --fst4w\n --ft8\n")))

    def test_unrunnable_binary_fails(self):
        self.assertFalse(decoder.jt9_decodes_msk144("jt9", run=self._run(exc=OSError(2, "x"))))

    def test_usage_text_is_recognised(self):
        self.assertTrue(decoder.is_jt9_usage_text(" Usage: jt9 [OPTIONS] file1 [file2 ...]\n"))
        self.assertFalse(decoder.is_jt9_usage_text("125900   3 +0.2 1500 & CQ K1ABC FN42\n"))


class TestBuildCmd(unittest.TestCase):

    def test_cmd_has_msk144_and_period(self):
        cmd = decoder.build_jt9_cmd("jt9", Path("/wd"), Path("/wd/slot.wav"))
        self.assertIn("--msk144", cmd)
        # -Y emits unresolved compound-call hashes as <NNNNNNN> so the
        # callhash table can resolve them (parity with wspr's FST4W path).
        self.assertIn("-Y", cmd)
        # -p 15 (the WSJT-X Guide's MSK144 period), -f 1500 (audio offset),
        # -F 200 and -d 3 (the Guide's tolerance and depth; jt9's own
        # defaults are +-20 Hz and Fast), -a workdir, wav last.
        self.assertEqual(cmd[cmd.index("-p") + 1], "15")
        self.assertEqual(cmd[cmd.index("-f") + 1], "1500")
        self.assertEqual(cmd[cmd.index("-F") + 1], "200")
        self.assertEqual(cmd[cmd.index("-d") + 1], "3")
        self.assertEqual(cmd[cmd.index("-a") + 1], "/wd")
        self.assertEqual(cmd[-1], "/wd/slot.wav")
        self.assertEqual(cmd[0], "jt9")

    def test_cmd_period_is_configurable(self):
        # The recorder passes its configured tr_period_sec (from the slot
        # cadence) as jt9's -p, so a 15 s VHF setup gets -p 15.
        cmd = decoder.build_jt9_cmd(
            "jt9", Path("/wd"), Path("/wd/slot.wav"), tr_period_sec=15)
        self.assertEqual(cmd[cmd.index("-p") + 1], "15")


class TestParseDecodedTxt(unittest.TestCase):

    def test_parses_a_decode_line(self):
        d = decoder.parse_decoded_txt_line("183000  10  0.5 1500 &  CQ K1ABC FN42")
        self.assertIsNotNone(d)
        self.assertEqual(d["snr"], 10)
        self.assertAlmostEqual(d["dt"], 0.5, places=2)
        self.assertEqual(d["audio_freq_hz"], 1500)
        self.assertEqual(d["sync"], "&")
        self.assertEqual(d["message"], "CQ K1ABC FN42")

    def test_tolerates_missing_sync_char(self):
        # If jt9's column layout omits a standalone sync char, the message
        # still parses (the sync defaults to the MSK144 indicator).
        d = decoder.parse_decoded_txt_line("183000  -3 -0.2 1623 K1ABC W9XYZ EN52")
        self.assertIsNotNone(d)
        self.assertEqual(d["snr"], -3)
        self.assertEqual(d["message"], "K1ABC W9XYZ EN52")

    def test_finished_sentinel_is_none(self):
        self.assertIsNone(
            decoder.parse_decoded_txt_line("<DecodeFinished>   0   0        0"))

    def test_blank_is_none(self):
        self.assertIsNone(decoder.parse_decoded_txt_line(""))
        self.assertIsNone(decoder.parse_decoded_txt_line("   "))

    def test_garbage_is_none(self):
        self.assertIsNone(decoder.parse_decoded_txt_line("not a decode line"))


class TestNormalizeLogLine(unittest.TestCase):

    def test_absolute_freq_and_utc(self):
        d = {"snr": 10, "dt": 0.5, "audio_freq_hz": 1500,
             "sync": "&", "message": "CQ K1ABC FN42"}
        # 2026-05-07 12:34:56 UTC
        slot = time.gmtime(1778157296)
        line = decoder.normalize_log_line(d, slot, 28_145_000)
        # abs RF = dial 28.145 MHz + 1500 Hz audio offset.
        self.assertIn(" 28146500 ", line)
        self.assertIn(" & CQ K1ABC FN42", line)
        self.assertTrue(line.startswith("2026/05/07 12:34:56 "))
        self.assertTrue(line.endswith("\n"))

    def test_roundtrips_through_ch_tailer(self):
        from meteor_scatter.core.ch_tailer import parse_decoder_line
        d = decoder.parse_decoded_txt_line("183015  -3 -0.2 1623 &  K1ABC W9XYZ EN52")
        line = decoder.normalize_log_line(d, time.gmtime(1778157296), 50_260_000)
        row = parse_decoder_line(line)
        self.assertEqual(row["mode"], "msk144")
        self.assertEqual(row["snr_db"], -3)
        self.assertEqual(row["frequency"], 50_261_623)   # 50.260 MHz + 1623 Hz
        self.assertEqual(row["rx_call"], "K1ABC")
        self.assertEqual(row["tx_call"], "W9XYZ")
        self.assertEqual(row["grid"], "EN52")


if __name__ == "__main__":
    unittest.main()
