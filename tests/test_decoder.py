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

    def test_explicit_override_wins_when_present(self):
        # The bundled jt9 path is a real file we can point at explicitly.
        bundled = REPO_ROOT / "bin" / "decoders" / "jt9-x86-v27"
        if not bundled.is_file():
            self.skipTest("bundled jt9-x86-v27 not present")
        self.assertEqual(decoder.resolve_jt9_binary(str(bundled)), str(bundled))

    def test_resolves_to_bundled_or_path(self):
        # With no override, resolution returns *something* (bundled binary
        # for this arch, or a bare 'jt9' fallback) — never empty.
        resolved = decoder.resolve_jt9_binary("")
        self.assertTrue(resolved)

    def test_missing_override_falls_back(self):
        # A non-existent override must not be returned verbatim; it falls
        # through to bundled/PATH resolution.
        resolved = decoder.resolve_jt9_binary("/no/such/jt9-binary")
        self.assertNotEqual(resolved, "/no/such/jt9-binary")


class TestBuildCmd(unittest.TestCase):

    def test_cmd_has_msk144_and_period(self):
        cmd = decoder.build_jt9_cmd("jt9", Path("/wd"), Path("/wd/slot.wav"))
        self.assertIn("--msk144", cmd)
        # -Y emits unresolved compound-call hashes as <NNNNNNN> so the
        # callhash table can resolve them (parity with wspr's FST4W path).
        self.assertIn("-Y", cmd)
        # -p 30 (default T/R period = stock WSJT-X MSK144), -f 1500 (audio
        # offset), -a workdir, wav last.
        self.assertEqual(cmd[cmd.index("-p") + 1], "30")
        self.assertEqual(cmd[cmd.index("-f") + 1], "1500")
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
