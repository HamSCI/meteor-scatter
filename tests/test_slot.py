"""Tests for SlotWorker: clock-driven slot harvesting + jt9 decoder lifecycle.

Slot-boundary math now lives in ka9q.SlotClock (see ka9q-python
tests/test_slot_clock.py); these tests cover the SlotWorker's use of it —
harvesting completed slots, extracting their sample window from the ring by
absolute offset, writing the WAV, and the hung-decoder reap path.
"""

import io
import json
import time
import threading
import tempfile
import unittest
from pathlib import Path

from unittest import mock

import numpy as np

from ka9q import SlotClock

from meteor_scatter.core.ring import Ring
from meteor_scatter.core.slot import SlotWorker
from meteor_scatter.core.wav import WavStats

SR = 12000


def _make_worker(tmpdir, *, mode="msk144", cadence=15.0, ring=None,
                 msqp=None, decoder_path="", capture="usb", get_anchor=None,
                 client_radiod=None, get_rtp_basis=None):
    """Construct a SlotWorker wired to a SlotClock + ring + latest-rtp box."""
    if ring is None:
        ring = Ring(max_seconds=60, sample_rate=SR,
                    dtype=np.complex64 if capture == "iq" else np.float32)
    clock = SlotClock(cadence_sec=cadence, sample_rate=SR, settle_sec=1.5)
    lock = threading.Lock()
    box = {"rtp": None}
    # get_anchor_utc_now returns the current UTC of the fixed anchor_rtp.  In
    # these tests there is no radiod slide, so it is just the anchored utc
    # (None until anchored).  ``box['anchor_utc']`` lets a test simulate a slide.
    def _anchor_utc_now():
        if "anchor_utc" in box:
            return box["anchor_utc"]
        return clock._anchor_utc  # None until clock.anchor() is called
    worker = SlotWorker(
        ring=ring,
        mode=mode,
        frequency_hz=28145000,
        cadence_sec=cadence,
        spool_dir=Path(tmpdir) / mode,
        log_fd=io.StringIO(),
        decoder_path=decoder_path,
        clock=clock,
        get_latest_rtp=lambda: box["rtp"],
        clock_lock=lock,
        get_anchor_utc_now=_anchor_utc_now,
        msqp=msqp,
        capture=capture,
        get_anchor=get_anchor,
        client_radiod=client_radiod,
        get_rtp_basis=get_rtp_basis,
    )
    return worker, clock, ring, box


class SlotHarvestTickTests(unittest.TestCase):
    """tick() harvests a completed slot and writes its WAV."""

    def test_tick_writes_wav_when_slot_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            # Anchor on a grid point (900000000 % 15 == 0) -> boundary0 @ off 0
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            # Fill 20 s of audio as 0.5 s chunks at contiguous offsets.
            for i in range(40):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            # latest RTP = just past newest sample (240000 samples in)
            box["rtp"] = clock.rtp_of_offset(40 * 6000)   # 240000 > 180000+settle

            # No decoder: with no MSQP archive a launch failure deletes the WAV.
            forked = []
            worker._fork_decoder = lambda wav, start: forked.append((wav, start))
            worker._tick()
            wavs = list((Path(tmpdir) / "msk144").glob("*.wav"))
            self.assertEqual([w for w, _ in forked], wavs)

            self.assertGreaterEqual(len(wavs), 1, "expected a WAV for slot0")
            # filename labels the epoch-aligned slot start (UTC 900000000)
            self.assertTrue(any("_28145.wav" in w.name for w in wavs))

    def test_tick_noop_before_anchor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            box["rtp"] = 123456            # latest set, but clock not anchored
            worker._tick()                 # must not raise / must do nothing
            self.assertEqual(
                list((Path(tmpdir) / "msk144").glob("*.wav")), [])

    def test_incomplete_slot_not_harvested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            # only 10 s of data — slot0 (15 s) + settle not yet complete
            for i in range(20):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(20 * 6000)   # 120000 < 198000
            worker._tick()
            self.assertEqual(
                list((Path(tmpdir) / "msk144").glob("*.wav")), [])


    def test_slot_window_follows_radiod_slide(self):
        """The extracted RTP window must track radiod's RTP↔UTC slide: for the
        SAME clean UTC boundary, a +1 s shift in the anchor's current UTC moves
        the window 1 s (12000 samples) earlier — i.e. we follow radiod instead
        of freezing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(
                tmpdir, decoder_path="/nonexistent/decode_msk144")
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            for i in range(80):                       # 40 s of audio
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(80 * 6000)   # latest_off = 480000
            captured: list[int] = []
            real = ring.extract_by_offset
            ring.extract_by_offset = lambda off, n: (captured.append(off)
                                                     or real(off, n))
            # No slide: boundary 900000015 → start_off (15 s)*12000 = 180000.
            worker._next_boundary_utc = 900_000_015.0
            box["anchor_utc"] = 900_000_000.0
            worker._tick()
            self.assertIn(180000, captured)
            # radiod's mapping of anchor_rtp slid +1.0 s → window 12000 earlier.
            captured.clear()
            worker._next_boundary_utc = 900_000_015.0
            box["anchor_utc"] = 900_000_001.0
            worker._tick()
            self.assertIn(168000, captured)


class _RenamedClock:
    """A SlotClock whose private ``_anchor_rtp`` has been renamed away (a ka9q-python update)."""
    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        if name == "_anchor_rtp":
            raise AttributeError(name)
        return getattr(self._real, name)


class AnchorRtpGuardTests(unittest.TestCase):
    """Final review M2: the harvest guard reads ka9q's private SlotClock._anchor_rtp.  If a
    ka9q-python update renames it, the worker must say so loudly, once, not cut no slots in
    silence."""

    def test_missing_anchor_rtp_logs_one_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            for i in range(40):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(40 * 6000)
            worker._clock = _RenamedClock(clock)
            worker._fork_decoder = lambda wav, start: None
            with self.assertLogs("meteor_scatter.core.slot", "ERROR") as cm:
                for _ in range(3):
                    worker._tick()
            errors = [r for r in cm.records if r.levelname == "ERROR"]
            self.assertEqual(len(errors), 1)
            self.assertIn("_anchor_rtp", errors[0].getMessage())
            self.assertEqual(list((Path(tmpdir) / "msk144").glob("*.wav")), [])

    def test_present_anchor_rtp_logs_no_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            box["rtp"] = 123456            # not anchored: _anchor_rtp exists and is None
            with self.assertNoLogs("meteor_scatter.core.slot", "ERROR"):
                worker._tick()


class DecodeTimeoutTests(unittest.TestCase):
    """A hung jt9 must be killed + reaped, not leaked forever.

    Regression for the unbounded _pending_procs / FD / WAV leak that
    otherwise grows until the MemoryMax cgroup OOM-kills the daemon.
    """

    def _worker(self, tmpdir):
        worker, _clock, _ring, _box = _make_worker(tmpdir)
        return worker

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
            # 4-tuple: (proc, wav_path, slot_start, fork_monotonic).
            # fork timestamp far enough in the past to be over any deadline.
            worker._pending_procs.append(
                (proc, wav, 0.0, time.monotonic() - 10_000)
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
            worker = self._worker(tmpdir)
            wav = Path(tmpdir) / "young.wav"
            wav.write_bytes(b"RIFFfake")
            proc = subprocess.Popen(
                ["sleep", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(SlotWorker._kill_proc, proc)
            worker._pending_procs.append((proc, wav, 0.0, time.monotonic()))
            worker._reap_finished()
            self.assertIsNone(proc.poll(), "in-deadline proc wrongly killed")
            self.assertEqual(len(worker._pending_procs), 1, "in-deadline proc dropped")
            self.assertEqual(worker.decodes_fail, 0, "false failure counted")


class Jt9StdoutHarvestTests(unittest.TestCase):
    """MSK144 decodes arrive on jt9's STDOUT, not decoded.txt (which stays
    empty for --msk144).  Regression: the daemon previously read decoded.txt
    and silently dropped every decode, so real pings never reached the log
    / sink.  These lock in the stdout-harvest path.
    """

    JT9_LINE = "000000   0  0.4 1509 &  CQ AC0G EM38"
    FINISHED = "<DecodeFinished>   0   0        0"

    def test_stdout_decode_normalized_to_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, _clock, _ring, _box = _make_worker(tmpdir)
            stdout = f"{self.JT9_LINE}\n{self.FINISHED}\n"
            worker._materialise_jt9_output(
                Path(tmpdir) / "x.wav", 900_000_000.0, stdout)
            out = worker._log_fd.getvalue()
            self.assertIn("CQ AC0G EM38", out)
            # abs RF = dial 28145000 + 1509 Hz audio offset
            self.assertIn(" 28146509 ", out)
            self.assertIn(" & CQ AC0G EM38", out)

    def test_finished_sentinel_only_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, _clock, _ring, _box = _make_worker(tmpdir)
            worker._materialise_jt9_output(
                Path(tmpdir) / "x.wav", 900_000_000.0, f"{self.FINISHED}\n")
            self.assertEqual(worker._log_fd.getvalue(), "")

    def test_read_proc_stdout_reads_real_pipe(self):
        import subprocess
        proc = subprocess.Popen(
            ["printf", "hello\nworld\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.wait()
        self.assertEqual(
            SlotWorker._read_proc_stdout(proc).splitlines(), ["hello", "world"])

    def test_usage_text_counts_as_a_failure_not_a_decode(self):
        """A jt9 that rejects the command line prints its usage and exits 0.
        That slot is a failure, logged once, never a silent empty decode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / "fake_jt9"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '-h' in sys.argv: print(' --msk144'); sys.exit(0)\n"
                "print(' Usage: jt9 [OPTIONS] file1 [file2 ...]')\n"
            )
            fake.chmod(0o755)
            worker, clock, ring, box = _make_worker(tmpdir, decoder_path=str(fake))
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            for i in range(40):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(40 * 6000)
            with self.assertLogs("meteor_scatter.core.slot", level="ERROR") as logs:
                worker._tick()
                worker._reap_all(wait=True)
            self.assertEqual(worker.decodes_ok, 0)
            self.assertGreaterEqual(worker.decodes_fail, 1)
            self.assertEqual(sum("printed its usage" in m for m in logs.output), 1)
            self.assertEqual(worker._log_fd.getvalue(), "")

    def test_end_to_end_fork_reap_harvests_stdout(self):
        """Fork a fake jt9 that prints a decode to stdout; the reap must
        harvest it into the log — the exact path the bug bypassed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / "fake_jt9"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '-h' in sys.argv: print(' --msk144'); sys.exit(0)\n"
                f"print({self.JT9_LINE!r})\n"
                f"print({self.FINISHED!r})\n"
            )
            fake.chmod(0o755)
            worker, clock, ring, box = _make_worker(
                tmpdir, decoder_path=str(fake))
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            for i in range(40):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(40 * 6000)
            worker._tick()
            worker._reap_all(wait=True)
            out = worker._log_fd.getvalue()
            self.assertIn("CQ AC0G EM38", out)
            self.assertIn(" 28146509 ", out)


class DesyncGuardTests(unittest.TestCase):
    """audit F18: a SlotClockDesyncError inside _tick's harvest must be
    caught and recovered via the on_desync callback (ChannelSink's
    _reset_timing) instead of re-raising as a 'SlotWorker tick error'
    every 500 ms forever with no recovery."""

    def test_tick_desync_fires_on_desync_and_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            fired = []
            worker._on_desync = lambda: fired.append(1)
            box["rtp"] = 2**30 + 10 * SR   # beyond the safe unwrap window
            worker._tick()                 # must not raise
            self.assertEqual(fired, [1])

    def test_tick_desync_without_callback_still_swallowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            worker._on_desync = None
            box["rtp"] = 2**30 + 10 * SR
            worker._tick()                 # must not raise


class _FakeMsqp:
    def __init__(self, fail=False):
        self.calls, self.slots, self.fail = [], [], fail
        self.scale_db = None

    def on_slot(self, slot_start, stats):
        self.slots.append(slot_start)

    def on_decoded(self, wav_path, slot_start, sr, stats, decodes):
        self.calls.append((wav_path, decodes))
        return not self.fail


class ArchiveDecisionTests(unittest.TestCase):
    LINE = "000000  -5  0.2 1500 & CQ AC0G EM38\n<DecodeFinished>\n"

    def _reap_with(self, stdout_text, msqp):
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=msqp)
            wav = Path(td) / "msk144" / "x.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"\0" * 64)
            w._slot_stats[wav] = WavStats(gain_db=30.0, clipped_samples=0, noise_dbfs=-80.0)
            proc = mock.MagicMock()
            proc.poll.return_value = 0
            proc.stdout.read.return_value = stdout_text.encode()
            proc.stderr.read.return_value = b""
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            w._reap_finished()
            return wav.exists()

    def test_decoded_slot_is_offered_to_the_archive(self):
        m = _FakeMsqp()
        self._reap_with(self.LINE, m)
        self.assertEqual(len(m.calls), 1)
        self.assertEqual(m.calls[0][1][0]["message"], "CQ AC0G EM38")

    def test_sentinel_only_output_is_not_archived(self):
        m = _FakeMsqp()
        still_there = self._reap_with("<DecodeFinished>\n", m)
        self.assertEqual(m.calls, [])
        self.assertFalse(still_there)

    def test_archive_failure_deletes_and_counts(self):
        m = _FakeMsqp(fail=True)
        still_there = self._reap_with(self.LINE, m)
        self.assertFalse(still_there)

    def test_shutdown_reap_offers_decoded_slot(self):
        m = _FakeMsqp(fail=True)
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=m)
            wav = Path(td) / "msk144" / "x.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"\0" * 64)
            w._slot_stats[wav] = WavStats(gain_db=30.0, clipped_samples=0, noise_dbfs=-80.0)
            proc = mock.MagicMock()
            proc.returncode = 0
            proc.stdout.read.return_value = self.LINE.encode()
            proc.stderr.read.return_value = b""
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            w._reap_all(wait=True)
            self.assertEqual(len(m.calls), 1)
            self.assertFalse(wav.exists())
            self.assertEqual(w._slot_stats, {})

    def test_spool_write_feeds_noise_watch_and_keeps_stats(self):
        m = _FakeMsqp()
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=m)
            wav = w._write_spool_wav(np.full(SR, 0.01, dtype=np.float32), 900_000_000.0)
            self.assertEqual(m.slots, [900_000_000.0])
            self.assertIsInstance(w._slot_stats[wav], WavStats)

    def test_decode_keeps_jt9_raw_line(self):
        m = _FakeMsqp()
        self._reap_with(self.LINE, m)
        self.assertEqual(m.calls[0][1][0]["jt9_line"], "000000  -5  0.2 1500 & CQ AC0G EM38")

    def test_raising_archive_logs_once_and_once_on_recovery(self):
        class Raising(_FakeMsqp):
            broken = True

            def on_decoded(self, *a):
                if self.broken:
                    raise RuntimeError("bug")
                return super().on_decoded(*a)
        m = Raising()
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=m)

            def reap():
                wav = Path(td) / "msk144" / "x.wav"
                wav.parent.mkdir(parents=True, exist_ok=True)
                wav.write_bytes(b"\0" * 64)
                w._slot_stats[wav] = WavStats(30.0, 0, -80.0)
                proc = mock.MagicMock()
                proc.poll.return_value = 0
                proc.stdout.read.return_value = self.LINE.encode()
                proc.stderr.read.return_value = b""
                w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
                w._reap_finished()
                return wav.exists()
            with self.assertLogs("meteor_scatter.core.slot", "WARNING") as cm:
                for _ in range(3):
                    self.assertFalse(reap())
            self.assertEqual(len([r for r in cm.records if "MSQP archive" in r.getMessage()]), 1)
            self.assertEqual(w.msqp_errors, 3)
            m.broken = False
            with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                reap(); reap()
            self.assertEqual(len([r for r in cm.records if "resumed" in r.getMessage()]), 1)

    def test_raising_noise_watch_logs_once(self):
        class Raising(_FakeMsqp):
            def on_slot(self, *a):
                raise ValueError("bad")
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=Raising())
            with self.assertLogs("meteor_scatter.core.slot", "WARNING") as cm:
                for i in range(3):
                    w._write_spool_wav(np.full(SR, 0.01, dtype=np.float32), 900_000_000.0 + 30 * i)
            self.assertEqual(len(cm.records), 1)

    def test_mutant_keep_everything_would_fail(self):
        # Guards the keep decision: a worker that archived every slot would call on_decoded here.
        m = _FakeMsqp()
        self._reap_with("", m)
        self.assertEqual(m.calls, [])


def _fake_jt9(tmpdir, help_text):
    fake = Path(tmpdir) / "fake_jt9"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '-h' in sys.argv:\n"
        f"    print({help_text!r})\n"
    )
    fake.chmod(0o755)
    return str(fake)


class AudioFormatTests(unittest.TestCase):
    """The spool WAV is F32 only for a jt9 that reads it."""

    def _fmt_bits(self, wav):
        import struct
        d = wav.read_bytes()
        tag, _ch, _sr, _br, _al, bits = struct.unpack_from("<HHIIHH", d, 20)
        return tag, bits

    def test_f32_for_the_port(self):
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 --msk-shmsg")
            w, *_ = _make_worker(td, decoder_path=jt9, capture="iq")
            self.assertTrue(w._audio_f32)
            wav = w._write_spool_wav(np.full(SR, 0.01, dtype=np.complex64), 900_000_000.0)
            self.assertEqual(self._fmt_bits(wav), (3, 32))

    def test_usb_stays_16_bit_with_the_port(self):
        # The usb path is the proof's control arm: installing the port must not change it.
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 --msk-shmsg")
            w, *_ = _make_worker(td, decoder_path=jt9)
            self.assertFalse(w._audio_f32)
            wav = w._write_spool_wav(np.full(SR, 0.01, dtype=np.float32), 900_000_000.0)
            self.assertEqual(self._fmt_bits(wav), (1, 16))

    def test_iq_with_302_writes_16_bit_and_keeps_the_iq(self):
        # Review Focus 4: WSJT-X 3.0.2 (no --msk-shmsg) reads 16-bit only; I/Q capture still
        # keeps the two-channel float I/Q beside the 16-bit audio.
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 -p")
            w, *_ = _make_worker(td, decoder_path=jt9, capture="iq")
            self.assertFalse(w._audio_f32)
            iq = np.full(SR, 0.01 + 0.02j, dtype=np.complex64)
            wav = w._write_spool_wav(iq, 900_000_000.0)
            self.assertEqual(self._fmt_bits(wav), (1, 16))
            iq_path = w._iq_path(wav)
            self.assertTrue(iq_path.exists())
            import struct
            tag, nch, _sr, _br, _al, bits = struct.unpack_from("<HHIIHH", iq_path.read_bytes(), 20)
            self.assertEqual((tag, nch, bits), (3, 2, 32))
            x = _read_f32_data(iq_path, 2)
            np.testing.assert_array_equal(x[:, 0], iq.real)
            np.testing.assert_array_equal(x[:, 1], iq.imag)

    def test_falls_back_to_s16_for_302(self):
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 -p")
            w, *_ = _make_worker(td, decoder_path=jt9)
            self.assertFalse(w._audio_f32)
            wav = w._write_spool_wav(np.full(SR, 0.01, dtype=np.float32), 900_000_000.0)
            self.assertEqual(self._fmt_bits(wav), (1, 16))

    def test_init_logs_the_audio_format_and_why(self):
        with tempfile.TemporaryDirectory() as td:
            for text, want in (("jt9 --msk144 --msk-shmsg", "F32"), ("jt9 --msk144 -p", "16-bit")):
                jt9 = _fake_jt9(td, text)
                with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                    _make_worker(td, decoder_path=jt9, capture="iq")
                line = [r.getMessage() for r in cm.records if "jt9 audio" in r.getMessage()]
                self.assertEqual(len(line), 1)
                self.assertIn(want, line[0])
                self.assertIn("--msk-shmsg", line[0])


def _decoding_jt9(tmpdir, help_text, line="000000  -5  0.2 1500 & CQ AC0G EM38"):
    """A fake jt9 that decodes `line` from any WAV, or prints help for -h."""
    fake = Path(tmpdir) / "decoding_jt9"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '-h' in sys.argv:\n"
        f"    print({help_text!r})\n"
        "else:\n"
        + (f"    print({line!r})\n" if line else "")
        + "    print('<DecodeFinished>   0   0        0')\n"
    )
    fake.chmod(0o755)
    return str(fake)


def _iq_slot(seconds=15, amp=0.01):
    t = np.arange(seconds * SR) / SR
    return (amp * np.exp(2j * np.pi * 1500.0 * t)).astype(np.complex64)


def _read_f32_data(path, channels):
    d = path.read_bytes()
    at = d.index(b"data") + 8
    x = np.frombuffer(d, dtype="<f4", offset=at)
    return x.reshape(-1, channels) if channels > 1 else x


def _iq_worker(td, *, decode=True, nonfinite=False):
    """An I/Q-capture worker with a real MsqpChannel, a fake jt9, and one slot written + forked."""
    from meteor_scatter.core.archive import Archive
    from meteor_scatter.core.frontend import FrontendLog
    from meteor_scatter.core.msqp import MsqpChannel
    from meteor_scatter.core.noise_watch import NoiseWatch
    jt9 = _decoding_jt9(td, "jt9 --msk144 --msk-shmsg", line=None if not decode else
                        "000000  -5  0.2 1500 & CQ AC0G EM38")
    ch = MsqpChannel(Archive(Path(td) / "arch", 90, 2), FrontendLog(), 28145000, "AC0G/B4",
                     None, NoiseWatch(None), "test", preset="iq", capture="iq")
    w, *_ = _make_worker(td, decoder_path=jt9, msqp=ch, capture="iq")
    return w, ch


def _run_slot(w, iq, t0=1797206370.0):
    wav = w._write_spool_wav(iq, t0)
    w._fork_decoder(wav, t0)
    for _ in range(200):
        w._reap_finished()
        if not w._pending_procs:
            break
        time.sleep(0.05)
    return wav


def _run_slot_at(w, samples, t0, start_off=None):
    wav = w._write_spool_wav(samples, t0, start_off)
    w._fork_decoder(wav, t0)
    for _ in range(200):
        w._reap_finished()
        if not w._pending_procs:
            break
        time.sleep(0.05)
    return wav


class IqCaptureTests(unittest.TestCase):
    def test_capture_default_usb(self):
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td)
            self.assertEqual(w._capture, "usb")
            wav = w._write_spool_wav(np.full(SR, 0.01, dtype=np.float32), 900_000_000.0)
            self.assertEqual(sorted(p.name for p in wav.parent.glob("*.wav")), [wav.name])

    def test_bad_capture_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                _make_worker(td, capture="lsb")

    def test_decoded_slot_keeps_iq(self):
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td)
            iq = _iq_slot()
            wav = _run_slot(w, iq)
            kept = Path(td) / "arch" / "10m" / "20261213" / "261213_235930.wav"
            self.assertTrue(kept.exists())
            x = _read_f32_data(kept, 2)
            self.assertEqual(x.shape, (15 * SR, 2))
            np.testing.assert_array_equal(x[:, 0], iq.real)        # radiod's scale, no gain
            np.testing.assert_array_equal(x[:, 1], iq.imag)
            import struct
            tag, nch, sr, _br, _al, bits = struct.unpack_from("<HHIIHH", kept.read_bytes(), 20)
            self.assertEqual((tag, nch, sr, bits), (3, 2, SR, 32))
            side = json.loads(kept.with_suffix(".json").read_text())
            self.assertEqual(side["capture"], "iq")
            self.assertEqual(side["iq_sample_rate"], SR)
            self.assertEqual(side["iq_encoding"], "f32le")
            self.assertEqual(side["iq_center_hz"], 28145000)
            self.assertEqual(side["iq_filter_hz"], [50, 3000])
            self.assertEqual(side["channel_requested"]["preset"], "iq")
            self.assertEqual(side["channel_requested"]["low_edge"], 50.0)
            self.assertEqual(side["channel_requested"]["high_edge"], 3000.0)
            self.assertNotIn("nonfinite_samples", side)
            self.assertFalse(wav.exists())                     # the audio file is gone
            self.assertEqual(list(wav.parent.glob("*.wav")), [])
            self.assertEqual(list(wav.parent.glob("*.iq.wav")), [])

    def test_undecoded_slot_keeps_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td, decode=False)
            wav = _run_slot(w, _iq_slot())
            self.assertFalse(wav.exists())
            self.assertFalse(wav.with_suffix(".iq.wav").exists())
            self.assertEqual(list(wav.parent.glob("*.wav")), [])
            self.assertEqual(list((Path(td) / "arch").rglob("*.wav")), [])
            self.assertEqual(w.decodes_ok, 1)

    def test_nonfinite_iq_slot(self):
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td)
            iq = _iq_slot()
            iq[1000] = complex(np.nan, 0.0)
            iq[2000] = complex(0.0, np.inf)
            iq[3000] = complex(np.inf, np.nan)
            with self.assertLogs("meteor_scatter.core.slot", "WARNING") as cm:
                wav = w._write_spool_wav(iq, 1797206370.0)
            warnings = [r for r in cm.records if "non-finite" in r.getMessage()]
            self.assertEqual(len(warnings), 1)
            self.assertIn("3", warnings[0].getMessage())
            # the audio jt9 reads is finite, with a finite gain
            audio = _read_f32_data(wav, 1)
            self.assertTrue(np.all(np.isfinite(audio)))
            self.assertTrue(np.isfinite(w._slot_stats[wav].gain_db))
            self.assertTrue(np.isfinite(w._slot_stats[wav].noise_dbfs))
            # jt9 still runs and the slot decodes
            w._fork_decoder(wav, 1797206370.0)
            for _ in range(200):
                w._reap_finished()
                if not w._pending_procs:
                    break
                time.sleep(0.05)
            self.assertEqual(w.decodes_ok, 1)
            # the kept I/Q is exactly as received, non-finite values included
            kept = Path(td) / "arch" / "10m" / "20261213" / "261213_235930.wav"
            x = _read_f32_data(kept, 2)
            self.assertTrue(np.isnan(x[1000, 0]))
            self.assertTrue(np.isinf(x[2000, 1]))
            side = json.loads(kept.with_suffix(".json").read_text())
            self.assertEqual(side["nonfinite_samples"], 3)
            self.assertTrue(side["iq_contains_nonfinite"])
            self.assertTrue(np.isfinite(side["wav_gain_db"]))
            self.assertTrue(np.isfinite(side["noise_floor_dbfs"]))

    def test_nonfinite_iq_undecoded_keeps_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td, decode=False)
            iq = _iq_slot()
            iq[5] = complex(np.nan, np.nan)
            wav = _run_slot(w, iq)
            self.assertEqual(list(wav.parent.glob("*.wav")), [])
            self.assertEqual(list((Path(td) / "arch").rglob("*.wav")), [])

    def test_nonfinite_audio_slot_in_usb_mode(self):
        # The audio path: a NaN peak must not turn every sample, or the gain, into NaN.
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 --msk-shmsg")
            w, *_ = _make_worker(td, decoder_path=jt9)
            x = np.full(SR, 0.01, dtype=np.float32)
            x[7] = np.nan
            with self.assertLogs("meteor_scatter.core.slot", "WARNING") as cm:
                wav = w._write_spool_wav(x, 900_000_000.0)
            self.assertEqual(len([r for r in cm.records if "non-finite" in r.getMessage()]), 1)
            self.assertTrue(np.isfinite(w._slot_stats[wav].gain_db))
            audio = _read_f32_data(wav, 1)
            self.assertTrue(np.all(np.isfinite(audio)))
            self.assertEqual(w._slot_stats[wav].nonfinite_samples, 1)

    def test_clean_slot_logs_no_nonfinite_warning(self):
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, capture="iq")
            with self.assertNoLogs("meteor_scatter.core.slot", "WARNING"):
                w._write_spool_wav(_iq_slot(1), 900_000_000.0)

    def test_iq_audio_matches_the_usb_audio_of_the_same_tone(self):
        with tempfile.TemporaryDirectory() as td:
            jt9 = _fake_jt9(td, "jt9 --msk144 --msk-shmsg")
            w, *_ = _make_worker(td, decoder_path=jt9, capture="iq", msqp=_FakeMsqp())
            iq = _iq_slot(1, amp=0.01)
            wav = w._write_spool_wav(iq, 900_000_000.0)
            audio = _read_f32_data(wav, 1)
            gain = 10.0 ** (w._slot_stats[wav].gain_db / 20.0)
            np.testing.assert_allclose(audio, iq.real * gain, rtol=1e-6)


class EpisodeLoggingTests(unittest.TestCase):
    def test_ten_nan_slots_log_one_warning_and_one_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, capture="iq")
            with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                for i in range(10):
                    iq = _iq_slot(1)
                    iq[3] = complex(np.nan, 0.0)
                    w._write_spool_wav(iq, 900_000_000.0 + 15 * i)
                w._write_spool_wav(_iq_slot(1), 900_000_000.0 + 150)      # a clean slot
                w._write_spool_wav(_iq_slot(1), 900_000_000.0 + 165)      # and another: silent
            lines = [(r.levelname, r.getMessage()) for r in cm.records
                     if "non-finite" in r.getMessage()]
            self.assertEqual([l for l, _ in lines], ["WARNING", "INFO"])
            self.assertIn("10 slots", lines[1][1])
            self.assertIn("10 samples", lines[1][1])

    def test_failed_iq_writes_log_once_then_recover(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, capture="iq")
            with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                with mock.patch("meteor_scatter.core.iq._commit", side_effect=OSError("full")):
                    for i in range(4):
                        w._write_spool_wav(_iq_slot(1), 900_000_000.0 + 15 * i)
                w._write_spool_wav(_iq_slot(1), 900_000_000.0 + 90)
            lines = [(r.levelname, r.getMessage()) for r in cm.records
                     if "I/Q" in r.getMessage() and "format" not in r.getMessage()]
            self.assertEqual([l for l, _ in lines], ["WARNING", "INFO"])
            self.assertIn("4 slots", lines[1][1])


class IqWriteFailureTests(unittest.TestCase):
    def test_failed_iq_write_decodes_and_archives_nothing(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td)
            with mock.patch("meteor_scatter.core.iq._commit", side_effect=OSError("full")):
                with self.assertLogs("meteor_scatter.core.slot", "WARNING"):
                    wav = _run_slot(w, _iq_slot())
            self.assertEqual(w.decodes_ok, 1)                       # jt9 still ran
            self.assertEqual(list((Path(td) / "arch").rglob("*")), [])
            self.assertEqual(list(wav.parent.glob("*.wav*")), [])    # no audio, no I/Q, no .part
            self.assertEqual(list(wav.parent.glob("*.part")), [])
            self.assertEqual(ch.archive_failures, 0)                # not even an archive attempt

    def test_audio_writer_failure_leaves_no_iq_behind(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td)
            with mock.patch("meteor_scatter.core.slot.write_wav_f32", side_effect=OSError("x")):
                with self.assertRaises(OSError):
                    w._write_spool_wav(_iq_slot(1), 1797206370.0)
            spool = Path(td) / "msk144"
            self.assertEqual([p for p in spool.glob("*") if p.is_file()], [])


class IqExitPathTests(unittest.TestCase):
    """Whatever ends a slot (jt9 exit code, a hung jt9, shutdown), no spool file stays and
    nothing is archived."""

    def _setup(self, td):
        w, ch = _iq_worker(td)
        wav = w._write_spool_wav(_iq_slot(2), 1797206370.0)
        self.assertTrue(wav.exists() and wav.with_suffix(".iq.wav").exists())
        return w, wav

    def _assert_clean(self, td, wav):
        self.assertEqual([p for p in wav.parent.glob("*") if p.is_file()], [])
        self.assertEqual(list((Path(td) / "arch").rglob("*")), [])

    def test_jt9_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            w, wav = self._setup(td)
            proc = mock.MagicMock()
            proc.poll.return_value = 3
            proc.stderr.read.return_value = b"boom"
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            with self.assertLogs("meteor_scatter.core.slot", "WARNING"):
                w._reap_finished()
            self.assertEqual(w.decodes_fail, 1)
            self._assert_clean(td, wav)

    def test_jt9_timeout_kill(self):
        with tempfile.TemporaryDirectory() as td:
            w, wav = self._setup(td)
            proc = mock.MagicMock()
            proc.poll.return_value = None
            w._pending_procs = [(proc, wav, 1797206370.0, time.monotonic() - 1000)]
            with self.assertLogs("meteor_scatter.core.slot", "WARNING"):
                w._reap_finished()
            proc.kill.assert_called()
            self._assert_clean(td, wav)

    def test_stop_with_a_decode_in_flight(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            w, wav = self._setup(td)
            proc = mock.MagicMock()
            proc.wait.side_effect = subprocess.TimeoutExpired("jt9", 5)
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            w.stop()
            self._assert_clean(td, wav)

    def test_stop_after_a_decode_finishes_archives_it(self):
        # The contrast: a decode that completes during shutdown IS kept, and the audio goes.
        with tempfile.TemporaryDirectory() as td:
            w, wav = self._setup(td)
            proc = mock.MagicMock()
            proc.returncode = 0
            proc.stdout.read.return_value = b"000000  -5  0.2 1500 & CQ AC0G EM38\n"
            proc.stderr.read.return_value = b""
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            w.stop()
            self.assertEqual(len(list((Path(td) / "arch").rglob("*.wav"))), 1)
            self.assertEqual([p for p in wav.parent.glob("*") if p.is_file()], [])


class SpoolSweepTests(unittest.TestCase):
    def test_sweep_removes_old_slot_files_only(self):
        import os
        from meteor_scatter.core.slot import sweep_spool
        with tempfile.TemporaryDirectory() as td:
            spool = Path(td)
            (spool / "work_28145").mkdir()
            (spool / "work_28145" / "keep.wav").write_bytes(b"x")
            names = ["a.wav", "b.iq.wav", "c.iq.wav.part", "d.wav", "e.txt"]
            for n in names:
                (spool / n).write_bytes(b"x")
            old = time.time() - 100
            for n in ("a.wav", "b.iq.wav", "c.iq.wav.part", "e.txt"):
                os.utime(spool / n, (old, old))
            self.assertEqual(sweep_spool(spool, 30.0), 3)
            left = sorted(p.name for p in spool.iterdir())
            self.assertEqual(left, ["d.wav", "e.txt", "work_28145"])
            self.assertTrue((spool / "work_28145" / "keep.wav").exists())

    def test_sweep_of_a_missing_dir_is_zero(self):
        from meteor_scatter.core.slot import sweep_spool
        self.assertEqual(sweep_spool(Path("/nonexistent/spool"), 30.0), 0)


class SidecarTimingTests(unittest.TestCase):
    """The timing block comes from the harvest snapshot.  Fixture: the anchor pinned at ANCHOR_RTP,
    the label mapping (anchor_utc_now) 0.5 ms past the original pin (a slide), the slot boundary
    T0 7.5 s after the label anchor, so the first sample is offset 90000 and T0 = label_anchor_utc
    + 90000/12000."""
    ANCHOR_RTP = 0xFFFFF000          # the first slot's RTP timestamp wraps past 2**32
    T0 = 1797206370.0
    LABEL_ANCHOR_UTC = T0 - 7.5
    PIN_UTC = LABEL_ANCHOR_UTC - 0.0005
    OFF = 90000

    @staticmethod
    def _anchor(utc, source="rtp_to_utc+authority", tier="T3"):
        import types
        from datetime import datetime, timezone
        from hamsci_dsp.timing import AnchorUTC
        snap = types.SimpleNamespace(
            governor_radiod="bee1.local", t_level_active=tier, sigma_ns=12345.0,
            utc_published=datetime(2026, 12, 13, 23, 59, 0, tzinfo=timezone.utc),
            host_clock={"verdict": "ok"})
        return AnchorUTC(utc=utc, source=source, offset_seconds=1.5e-6,
                         offset_ns=1500, snapshot=snap, rtp_referenced=True)

    def _worker(self, td, capture, during_extract=None):
        from meteor_scatter.core.archive import Archive
        from meteor_scatter.core.frontend import FrontendLog, FrontendReading
        from meteor_scatter.core.msqp import MsqpChannel
        from meteor_scatter.core.noise_watch import NoiseWatch
        jt9 = _decoding_jt9(td, "jt9 --msk144 --msk-shmsg")
        fe = FrontendLog()
        fe.add(FrontendReading(self.T0 - 1, 10.0, 0.0, True, -23.5, 0))
        fe.add(FrontendReading(self.T0 + 5, 7.0, 3.0, True, -23.5, 0))     # a step inside the slot
        ch = MsqpChannel(Archive(Path(td) / "arch", 90, 2), fe, 28145000, "AC0G/B4", 11.0,
                         NoiseWatch(None), "test", preset="iq" if capture == "iq" else "usb",
                         capture=capture)
        self.anchor = self._anchor(self.PIN_UTC)
        self.cur_anchor = {"a": self.anchor}
        self.basis = {"b": "delivered_rtp_start"}
        w, clock, ring, box = _make_worker(td, decoder_path=jt9, msqp=ch, capture=capture,
                                           get_anchor=lambda: self.cur_anchor["a"],
                                           client_radiod="rx.local",
                                           get_rtp_basis=lambda: self.basis["b"])
        clock.anchor(self.ANCHOR_RTP, self.PIN_UTC)
        box["anchor_utc"] = self.LABEL_ANCHOR_UTC
        n = 330000
        if capture == "iq":
            ring.push(_iq_slot(1)[:SR].repeat(28)[:n].astype(np.complex64), 0)
        else:
            ring.push(np.full(n, 0.01, dtype=np.float32), 0)
        box["rtp"] = (self.ANCHOR_RTP + self.OFF + 180000 + 18000 + 10) & 0xFFFFFFFF
        if during_extract is not None:
            real = ring.extract_by_offset

            def hook(*a, **k):
                during_extract(clock, box)
                return real(*a, **k)
            ring.extract_by_offset = hook
        return w, clock

    def _run(self, td, capture, during_extract=None):
        w, clock = self._worker(td, capture, during_extract)
        w._tick()
        for _ in range(200):
            w._reap_finished()
            if not w._pending_procs:
                break
            time.sleep(0.05)
        kept = Path(td) / "arch" / "10m" / "20261213" / "261213_235930.json"
        return json.loads(kept.read_text())

    def _check(self, side):
        t = side["timing"]
        sr = t["sample_rate"]
        self.assertEqual(sr, SR)
        self.assertEqual(t["slot_start_utc_exact"], self.T0)
        self.assertEqual(t["anchor_rtp"], self.ANCHOR_RTP)
        self.assertEqual(t["first_sample_offset"], self.OFF)
        self.assertEqual(t["first_sample_rtp"], (self.ANCHOR_RTP + self.OFF) & 0xFFFFFFFF)
        self.assertEqual(t["label_anchor_utc"], self.LABEL_ANCHOR_UTC)
        self.assertEqual(t["batch_rtp_basis"], "delivered_rtp_start")
        self.assertTrue(any("batch_rtp_basis" in n for n in t["notes"]))
        # the relation the notes state: sample 0's UTC reproduces the slot start to a sample
        utc0 = t["label_anchor_utc"] + t["first_sample_offset"] / sr
        self.assertLessEqual(abs(utc0 - t["slot_start_utc_exact"]), 0.5 / sr)
        self.assertEqual(utc0, self.T0)
        # the slide since the original pin
        self.assertAlmostEqual(t["label_anchor_utc"] - t["anchor"]["utc"], 0.0005, places=6)
        self.assertEqual(t["anchor"], {"utc": self.PIN_UTC, "source": "rtp_to_utc+authority",
                                       "rtp_referenced": True, "offset_ns": 1500})
        self.assertTrue(any("label_anchor_utc + (first_sample_offset + i)" in n
                            for n in t["notes"]))
        self.assertTrue(any("within half a sample" in n for n in t["notes"]))
        applied = t["timing_authority_applied"]
        want = self.anchor.timing_authority_applied("rx.local")
        for k in ("source", "tier", "sigma_ns", "radiod_id", "rtp_to_utc_offset_ns",
                  "anchor_source", "anchor_utc", "host_clock_verdict", "authority_utc_published"):
            self.assertEqual(applied[k], want[k], k)
        self.assertEqual((applied["tier"], applied["sigma_ns"], applied["radiod_id"]),
                         ("T3", 12345.0, "rx.local"))
        self.assertIsNotNone(applied["snapshot_age_s"])
        # the gain stages
        self.assertEqual(side["scale_db"], 11.0)
        self.assertEqual(side["frontend"]["start"]["rf_gain"], 10.0)
        self.assertEqual(side["frontend"]["steps"][0]["to_db"], 7.0)
        chain = side["gain_chain"]
        for stage in chain["order"]:
            self.assertIn(stage["key"], side)
        self.assertEqual([st["key"] for st in chain["order"]],
                         ["frontend", "channel_reported", "scale_db", "wav_gain_db"])
        self.assertIn("channel_requested", side)

    def test_a_reanchor_after_harvest_does_not_change_the_slots_timing(self):
        def reanchor(clock, box):
            # A stream restore between harvest and sidecar: new anchor RTP, new anchor object.
            clock.anchor(0x12345678, self.T0 + 1000.0)
            self.cur_anchor["a"] = self._anchor(self.T0 + 1000.0, source="wallclock_fallback",
                                                tier="T4")
            box["anchor_utc"] = self.T0 + 1000.0
        with tempfile.TemporaryDirectory() as td:
            self._check(self._run(td, "iq", during_extract=reanchor))

    def test_iq_sidecar_carries_the_injected_timing(self):
        with tempfile.TemporaryDirectory() as td:
            side = self._run(td, "iq")
            self._check(side)
            self.assertIn("no gain of its own", side["gain_chain"]["stored_recording"])
            self.assertEqual(side["gain_chain"]["stored_recording_gain_db"], 0.0)

    def test_usb_sidecar_carries_the_injected_timing(self):
        with tempfile.TemporaryDirectory() as td:
            side = self._run(td, "usb")
            self._check(side)
            self.assertNotIn("capture", side)
            self.assertIn("wav_gain_db", side["gain_chain"]["stored_recording"])

    def test_sidecar_without_an_anchor_still_has_the_exact_start(self):
        with tempfile.TemporaryDirectory() as td:
            w, ch = _iq_worker(td)
            _run_slot(w, _iq_slot())
            kept = Path(td) / "arch" / "10m" / "20261213" / "261213_235930.json"
            t = json.loads(kept.read_text())["timing"]
            self.assertEqual(t["slot_start_utc_exact"], 1797206370.0)
            self.assertEqual(t["sample_rate"], SR)
            self.assertIsNone(t["anchor"])
            self.assertIsNone(t["timing_authority_applied"])
            self.assertIsNone(t["first_sample_offset"])
            self.assertIsNone(t["batch_rtp_basis"])


class InsufficientSamplesEpisodeTests(unittest.TestCase):
    def test_empty_slots_log_once_then_clear(self):
        with tempfile.TemporaryDirectory() as td:
            w, clock, ring, box = _make_worker(td)
            box["anchor_utc"] = 1797206370.0 - 7.5
            clock.anchor(1000, 1797206370.0 - 7.5)
            # latest_rtp far enough that three slots are due, but the ring is empty
            box["rtp"] = 1000 + 7 * 15 * SR
            with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                w._tick()
            warns = [r for r in cm.records if "insufficient" in r.getMessage()]
            self.assertEqual(len(warns), 1)
            self.assertGreaterEqual(w.slots_empty, 3)
            # the ring catches up: the next harvested slot is complete, one recovery line
            for k in range(30):                       # in chunks: one oversize chunk would evict itself
                ring.push(np.full(60000, 0.01, dtype=np.float32), k * 60000)
            box["rtp"] = 1000 + 9 * 15 * SR
            with self.assertLogs("meteor_scatter.core.slot", "INFO") as cm:
                w._tick()
            self.assertEqual(len([r for r in cm.records if "complete again" in r.getMessage()]), 1)


if __name__ == "__main__":
    unittest.main()
