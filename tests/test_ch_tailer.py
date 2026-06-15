"""Tests for meteor_scatter.core.ch_tailer (CONTRACT v0.6 §17 wiring).

Covers:
  - parse_jt9_msk144_line: the normalized MSK144 log-line format that
    slot.py emits from jt9's decoded.txt (see core.decoder.normalize_log_line)
  - callhash.parse_message: best-effort callsign/grid/report extraction
    (shared across all recorders) + compound-callsign hash substitution
  - ChTailer: tail/insert flow with a fake writer (no sink server needed)
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from callhash import CallHashTable, hash22, parse_message
from meteor_scatter.core.ch_tailer import (
    ChTailer,
    parse_jt9_msk144_line,
    parse_decoder_line,
)


# Normalized MSK144 log lines as slot.py writes them:
#   "YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>"
# (core.decoder.normalize_log_line).  The frequency is the absolute RF
# frequency in Hz (channel dial + jt9 audio offset).  The parser tolerates
# locale grouping in the freq token too.
LINE_PLAIN   = "2026/05/07 12:34:56 -15 +0.50 28131500 & K1ABC W1XYZ EM26"
LINE_GROUPED = "2026/05/07 12:34:56 -15 +0.50 28,131,500 & K1ABC W1XYZ EM26"
LINE_MSK144  = LINE_PLAIN

# Every row dict the MSK144 parser emits carries this fixed key set, so
# the msk144.spots schema columns map to stable keys.
EXPECTED_ROW_KEYS = {
    "time", "mode", "decoder_kind",
    "score", "snr_db", "spectral_width_hz",
    "dt", "frequency", "frequency_mhz",
    "message", "tx_call", "rx_call", "grid", "report",
}


class TestLineParser(unittest.TestCase):

    def test_full_line_plain(self):
        row = parse_jt9_msk144_line(LINE_PLAIN, mode="msk144")
        self.assertIsNotNone(row)
        self.assertEqual(row["mode"], "msk144")
        self.assertEqual(row["snr_db"], -15)
        self.assertIsNone(row["score"])          # jt9 reports dB, not a score
        self.assertAlmostEqual(row["dt"], 0.50, places=2)
        self.assertEqual(row["frequency"], 28_131_500)
        self.assertAlmostEqual(row["frequency_mhz"], 28.1315, places=4)
        self.assertEqual(row["message"], "K1ABC W1XYZ EM26")
        self.assertEqual(
            row["time"], datetime(2026, 5, 7, 12, 34, 56, tzinfo=timezone.utc)
        )

    def test_full_line_grouped_freq(self):
        # Tolerate a thousands-separated frequency token.
        row = parse_jt9_msk144_line(LINE_GROUPED, mode="msk144")
        self.assertIsNotNone(row)
        self.assertEqual(row["frequency"], 28_131_500)

    def test_blank_line_returns_none(self):
        self.assertIsNone(parse_jt9_msk144_line("", mode="msk144"))
        self.assertIsNone(parse_jt9_msk144_line("   ", mode="msk144"))

    def test_no_sep_returns_none(self):
        self.assertIsNone(parse_jt9_msk144_line(
            "2026/05/07 12:34:56 -15 +0.50 28131500 K1ABC W1XYZ EM26",
            mode="msk144"))

    def test_short_line_returns_none(self):
        self.assertIsNone(parse_jt9_msk144_line(
            "2026/05/07 & junk", mode="msk144"))

    def test_garbled_freq_returns_none(self):
        bad = LINE_PLAIN.replace("28131500", "not-a-number")
        self.assertIsNone(parse_jt9_msk144_line(bad, mode="msk144"))

    def test_unparseable_message_keeps_raw_text(self):
        line = "2026/05/07 12:34:56 -15 +0.50 28131500 & ??random gibberish??"
        row = parse_jt9_msk144_line(line, mode="msk144")
        self.assertIsNotNone(row)
        self.assertEqual(row["message"], "??random gibberish??")
        self.assertEqual(row["tx_call"], "")    # parse failed but row still emitted


class TestMessageParser(unittest.TestCase):
    """The shared callhash.parse_message is what every recorder uses.

    It always returns the full key set (``message`` + the four call
    fields), with empty/None defaults rather than omitting keys.
    """

    def test_simple_first_contact(self):
        out = parse_message("K1ABC W1XYZ EM26")
        self.assertEqual(out["rx_call"], "K1ABC")
        self.assertEqual(out["tx_call"], "W1XYZ")
        self.assertEqual(out["grid"], "EM26")
        self.assertIsNone(out["report"])

    def test_six_char_grid(self):
        out = parse_message("K1ABC W1XYZ EM26ov")
        self.assertEqual(out["grid"], "EM26ov")

    def test_signal_report(self):
        out = parse_message("K1ABC W1XYZ -15")
        self.assertEqual(out["rx_call"], "K1ABC")
        self.assertEqual(out["tx_call"], "W1XYZ")
        self.assertEqual(out["report"], -15)

    def test_signed_positive_report(self):
        out = parse_message("K1ABC W1XYZ +05")
        self.assertEqual(out["report"], 5)

    def test_roger_report(self):
        out = parse_message("K1ABC W1XYZ R-15")
        self.assertEqual(out["report"], -15)

    def test_cq_message(self):
        out = parse_message("CQ K1ABC FN42")
        self.assertEqual(out["tx_call"], "K1ABC")
        self.assertEqual(out["grid"], "FN42")
        self.assertEqual(out["rx_call"], "")

    def test_cq_with_target(self):
        # "CQ DX K1ABC FN42" — "DX" is a region tag (not a callsign).
        # The parser scans past it and pulls K1ABC as the tx (sender).
        out = parse_message("CQ DX K1ABC FN42")
        self.assertEqual(out["tx_call"], "K1ABC")
        self.assertEqual(out["grid"], "FN42")

    def test_freeform_returns_empty_calls(self):
        out = parse_message("hello world")
        self.assertEqual(out["tx_call"], "")
        self.assertEqual(out["rx_call"], "")
        self.assertEqual(out["grid"], "")
        self.assertIsNone(out["report"])
        self.assertEqual(out["message"], "hello world")

    def test_call_with_slash_suffix(self):
        out = parse_message("K1ABC/QRP W1XYZ FN42")
        self.assertEqual(out["rx_call"], "K1ABC/QRP")


class TestHashResolution(unittest.TestCase):
    """End-to-end: a numeric hash from jt9 -Y is resolved back to the
    compound call once we've observed its announcement."""

    def test_numeric_hash_resolved_after_announcement(self):
        table = CallHashTable()
        table.observe("<PJ4/K1ABC> CQ")
        h = hash22("PJ4/K1ABC")
        line = f"2026/05/07 12:34:56 -15 +0.50 28131500 & <{h:07d}> W1XYZ R-12"
        row = parse_jt9_msk144_line(line, mode="msk144", table=table)
        self.assertIsNotNone(row)
        self.assertEqual(row["rx_call"], "PJ4/K1ABC")     # substituted, not dropped
        self.assertEqual(row["tx_call"], "W1XYZ")
        self.assertEqual(row["report"], -12)
        self.assertNotIn("<", row["message"])

    def test_unknown_hash_left_as_placeholder(self):
        table = CallHashTable()
        line = "2026/05/07 12:34:56 -15 +0.50 28131500 & <1234567> W1XYZ 73"
        row = parse_jt9_msk144_line(line, mode="msk144", table=table)
        self.assertIsNotNone(row)
        self.assertEqual(row["rx_call"], "")              # unrecoverable
        self.assertEqual(row["tx_call"], "W1XYZ")
        self.assertIn("<1234567>", row["message"])        # raw preserved


class TestDecoderLineRouter(unittest.TestCase):
    """`parse_decoder_line` routes MSK144 lines and rejects junk."""

    def test_routes_msk144_format(self):
        row = parse_decoder_line(LINE_MSK144, mode="msk144")
        self.assertIsNotNone(row)
        self.assertEqual(row["decoder_kind"], "jt9")
        self.assertEqual(row["mode"], "msk144")

    def test_default_mode_is_msk144(self):
        # Router called without a mode hint tags rows msk144.
        row = parse_decoder_line(LINE_MSK144)
        self.assertEqual(row["mode"], "msk144")

    def test_unrecognised_returns_none(self):
        self.assertIsNone(parse_decoder_line("hello world"))
        self.assertIsNone(parse_decoder_line(""))
        # 4-digit numeric prefix but not the YYYY/MM/DD shape — also rejected.
        self.assertIsNone(parse_decoder_line("1234 something else"))


class TestMeteorScatterRowShape(unittest.TestCase):
    """The MSK144 parser populates the fixed msk144.spots key set."""

    def test_row_has_all_keys(self):
        row = parse_jt9_msk144_line(LINE_MSK144, mode="msk144")
        self.assertEqual(set(row.keys()), EXPECTED_ROW_KEYS)

    def test_snr_db_is_set_score_is_none(self):
        """jt9 reports a calibrated dB SNR (snr_db); the decode_ft8-era
        `score` field is the documented None sentinel for this client,
        and spectral_width_hz is not surfaced for MSK144."""
        row = parse_jt9_msk144_line(LINE_MSK144, mode="msk144")
        self.assertEqual(row["snr_db"], -15)         # real dB from jt9
        self.assertIsNone(row["score"])              # not an ft8_lib score
        self.assertIsNone(row["spectral_width_hz"])


# ── ChTailer with a fake writer ─────────────────────────────────────────────

class FakeWriter:
    def __init__(self, noop=False):
        self._noop = noop
        self.health = "noop" if noop else "ok"
        self.inserts: list = []
        self.flushed = 0
        self.closed = False

    @property
    def is_noop(self):
        return self._noop

    def insert(self, rows):
        self.inserts.extend(rows)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True


class TestChTailer(unittest.TestCase):

    def _make_tailer(self, log_path: Path, *, noop=False, **kw):
        fake = FakeWriter(noop=noop)
        tailer = ChTailer(
            log_path=log_path, mode="msk144", radiod_id="test-rx888",
            host_call="AC0G", host_grid="EM38ww",
            processing_version="0.1.0+abc",
            writer_factory=lambda batch_rows: fake,
            **kw,
        )
        return tailer, fake

    def test_noop_mode_starts_thread_but_inserts_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(log_path, noop=True)
            tailer.start()
            try:
                log_path.write_text(LINE_PLAIN + "\n")
                time.sleep(0.05)         # is_noop short-circuits in poll
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(fake.inserts, [])
            self.assertEqual(tailer.health, "noop")

    def test_skips_history_at_startup(self):
        """A line written before .start() should NOT be replayed."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text(LINE_PLAIN + "\n")
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                time.sleep(1.5)
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(fake.inserts, [],
                             "tailer should not replay pre-existing log content")

    def test_consumes_appended_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")            # empty start
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                    f.write(LINE_GROUPED + "\n")
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and len(fake.inserts) < 2:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(len(fake.inserts), 2)
            for row in fake.inserts:
                self.assertEqual(row["host_call"], "AC0G")
                self.assertEqual(row["host_grid"], "EM38ww")
                self.assertEqual(row["radiod_id"], "test-rx888")
                self.assertEqual(row["processing_version"], "0.1.0+abc")
                self.assertEqual(row["mode"], "msk144")

    def test_forward_flag_passes_through_unchanged(self):
        """forward_to_pskreporter is a vestigial per-row flag (always False
        in the deposit-only build); it passes through to the row unchanged."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(
                log_path, forward_to_pskreporter=False,
            )
            tailer.start()
            try:
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and len(fake.inserts) < 1:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertGreaterEqual(len(fake.inserts), 1)
            for row in fake.inserts:
                self.assertEqual(row["forward_to_pskreporter"], False)

    def test_handles_log_rotation(self):
        """File-shrunk-below-last-pos → tailer resets to head and replays."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and len(fake.inserts) < 1:
                    time.sleep(0.1)
                self.assertGreaterEqual(len(fake.inserts), 1)
                log_path.write_text("")
                time.sleep(1.5)
                log_path.write_text(LINE_GROUPED + "\n")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and len(fake.inserts) < 2:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertGreaterEqual(len(fake.inserts), 2)


class TestChTailerReporterId(unittest.TestCase):
    """Phase-3 (sigmond MULTI-INSTANCE-ARCHITECTURE.md §7): spot rows
    carry a `reporter_id` field, falling back to `radiod_id` when no
    explicit reporter ID is provided (legacy single-instance world)."""

    def _make_tailer_with_writes(self, reporter_id=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            fake = FakeWriter()
            tailer = ChTailer(
                log_path=log_path, mode="msk144",
                radiod_id="test-rx888",
                reporter_id=reporter_id,
                host_call="AC0G", host_grid="EM38ww",
                processing_version="0.1.0+abc",
                writer_factory=lambda batch_rows: fake,
                forward_to_pskreporter=False,
            )
            tailer.start()
            try:
                time.sleep(0.6)
                with open(log_path, "a") as fh:
                    fh.write(LINE_PLAIN + "\n")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not fake.inserts:
                    time.sleep(0.05)
            finally:
                tailer.stop(timeout=2.0)
            return fake.inserts

    def test_reporter_id_falls_back_to_radiod_id_when_none(self):
        inserts = self._make_tailer_with_writes(reporter_id=None)
        self.assertTrue(inserts, "expected at least one row inserted")
        row = inserts[0]
        self.assertEqual(row["reporter_id"], "test-rx888")
        self.assertEqual(row["instance"], "test-rx888")  # legacy field preserved

    def test_reporter_id_uses_explicit_value(self):
        inserts = self._make_tailer_with_writes(reporter_id="AC0G=S")
        self.assertTrue(inserts, "expected at least one row inserted")
        row = inserts[0]
        self.assertEqual(row["reporter_id"], "AC0G=S")
        self.assertEqual(row["instance"], "test-rx888")
        self.assertEqual(row["radiod_id"], "test-rx888")


if __name__ == "__main__":
    unittest.main()
