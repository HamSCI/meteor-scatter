"""Tests for SlotWorker cadence alignment and jt9 decoder invocation."""

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from msk144_recorder.core.ring import Ring
from msk144_recorder.core.slot import SlotWorker


class CadenceAlignmentTests(unittest.TestCase):
    """Verify slot boundary math (MSK144 = 15 s T/R period)."""

    def test_msk144_alignment(self):
        worker = self._make_worker(cadence=15.0)
        # 1005 = 67*15, a true 15 s boundary
        self.assertEqual(worker._align_to_cadence(1005.0), 1005.0)
        self.assertEqual(worker._align_to_cadence(1005.1), 1020.0)
        self.assertEqual(worker._align_to_cadence(1019.9), 1020.0)
        self.assertEqual(worker._align_to_cadence(0.0), 0.0)
        self.assertEqual(worker._align_to_cadence(15.0), 15.0)
        self.assertEqual(worker._align_to_cadence(14.9), 15.0)

    def test_alignment_at_epoch_boundaries(self):
        worker = self._make_worker(cadence=15.0)
        self.assertEqual(worker._align_to_cadence(0.0), 0.0)
        self.assertEqual(worker._align_to_cadence(15.0), 15.0)
        self.assertEqual(worker._align_to_cadence(14.9), 15.0)

    def _make_worker(self, cadence):
        ring = Ring(max_seconds=30, sample_rate=12000)
        tmpdir = tempfile.mkdtemp()
        import io
        return SlotWorker(
            ring=ring,
            mode="msk144",
            frequency_hz=28130000,
            cadence_sec=cadence,
            spool_dir=Path(tmpdir),
            log_fd=io.StringIO(),
            decoder_path="",        # auto-resolve bundled jt9
        )


class SlotExtractionTickTests(unittest.TestCase):
    """Verify the tick() → extract_slot → wav → decoder pipeline."""

    def test_tick_writes_wav_when_slot_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ring = Ring(max_seconds=60, sample_rate=12000)

            # Start at a 15 s boundary and fill ~35 s of data so there's
            # at least one complete 15 s slot + settle time.
            base_utc = math.ceil(1000.0 / 15) * 15  # = 1005
            for i in range(70):
                t = base_utc + i * 0.5
                ring.push(np.zeros(6000, dtype=np.float32), t)
            # ring covers 1005.0..1040.0 — head_utc ≈ 1040.0
            # First slot 1005.0-1020.0; need head > 1020 + 1.5 settle ✓

            import io
            log_buf = io.StringIO()

            worker = SlotWorker(
                ring=ring,
                mode="msk144",
                frequency_hz=28130000,
                cadence_sec=15.0,
                spool_dir=Path(tmpdir) / "msk144",
                log_fd=log_buf,
                decoder_path="",      # auto-resolve bundled jt9
                keep_wav=True,
            )

            worker._tick()                       # sets _next_slot_start
            self.assertIsNotNone(worker._next_slot_start)
            worker._tick()                       # extract + write wav + fork jt9

            wav_files = list((Path(tmpdir) / "msk144").glob("*.wav"))
            self.assertGreaterEqual(len(wav_files), 1, "Expected at least one WAV file")
            # Clean up any jt9 children the fork launched.
            worker._reap_all(wait=True)


class DecodeTimeoutTests(unittest.TestCase):
    """A hung jt9 must be killed + reaped, not leaked forever.

    Regression for the unbounded _pending_procs / FD / WAV leak that
    otherwise grows until the MemoryMax cgroup OOM-kills the daemon.
    """

    def _worker(self, tmpdir, keep_wav=False):
        import io
        return SlotWorker(
            ring=None,
            mode="msk144",
            frequency_hz=28130000,
            cadence_sec=15.0,
            spool_dir=Path(tmpdir),
            log_fd=io.StringIO(),
            decoder_path="",
            keep_wav=keep_wav,
        )

    def test_hung_decode_killed_past_deadline(self):
        import os
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(tmpdir)
            wav = Path(tmpdir) / "hung.wav"
            wav.write_bytes(b"RIFFfake")
            proc = subprocess.Popen(
                ["sleep", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(SlotWorker._kill_proc, proc)
            fds_before = len(os.listdir("/proc/%d/fd" % os.getpid()))
            # 5-tuple: (proc, wav_path, slot_start, fork_monotonic, prev_lines).
            # fork timestamp far enough in the past to be over any deadline.
            worker._pending_procs.append(
                (proc, wav, 0.0, time.monotonic() - 10_000, 0)
            )
            worker._reap_finished()
            time.sleep(0.2)
            self.assertIsNotNone(proc.poll(), "hung proc was not killed")
            self.assertLess(proc.returncode, 0, "expected death by signal")
            self.assertEqual(len(worker._pending_procs), 0, "not dropped from pending")
            self.assertEqual(worker.decodes_fail, 1)
            self.assertFalse(wav.exists(), "spool wav not cleaned up")
            fds_after = len(os.listdir("/proc/%d/fd" % os.getpid()))
            self.assertLessEqual(fds_after, fds_before, "FD leak on kill path")

    def test_in_deadline_decode_left_pending(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(tmpdir, keep_wav=True)
            wav = Path(tmpdir) / "young.wav"
            wav.write_bytes(b"RIFFfake")
            proc = subprocess.Popen(
                ["sleep", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(SlotWorker._kill_proc, proc)
            worker._pending_procs.append((proc, wav, 0.0, time.monotonic(), 0))
            worker._reap_finished()
            self.assertIsNone(proc.poll(), "in-deadline proc wrongly killed")
            self.assertEqual(len(worker._pending_procs), 1, "in-deadline proc dropped")
            self.assertEqual(worker.decodes_fail, 0, "false failure counted")


if __name__ == "__main__":
    unittest.main()
