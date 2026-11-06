"""One F32 I/Q channel: the audio made from it, the file kept from it, the ring that holds it."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from meteor_scatter.core.iq import iq_to_usb_audio, write_iq_wav
from meteor_scatter.core.ring import Ring

SR = 12000


def _tone(freq_hz, seconds=1.0, amp=0.25):
    """A one-sided complex tone: radiod's filter is the one that makes the I/Q one-sided."""
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.exp(2j * np.pi * freq_hz * t)).astype(np.complex64)


def _amp_at(x, freq_hz):
    """Amplitude of the real tone at freq_hz (a whole number of cycles, so no leakage)."""
    t = np.arange(len(x)) / SR
    ref = np.exp(-2j * np.pi * freq_hz * t)
    return 2.0 * abs(np.sum(x.astype(np.float64) * ref)) / len(x)


class IqToUsbAudioTests(unittest.TestCase):
    def test_iq_to_usb_audio_tone(self):
        # radiod's mono USB output is gain * Re(iq) (ka9q-radio linear.c), so a one-sided
        # +1500 Hz tone of amplitude A becomes a real 1500 Hz tone of amplitude A.
        audio = iq_to_usb_audio(_tone(+1500.0, amp=0.25))
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.shape, (SR,))
        self.assertAlmostEqual(_amp_at(audio, 1500.0), 0.25, places=5)

    def test_opposite_sideband_is_not_preserved_as_a_different_tone(self):
        # A tone at -1500 Hz is outside the channel's passband, so radiod never delivers one.
        # Built anyway, it shows up at 1500 Hz in the real part, at the same amplitude: the
        # audio has no memory of the sign, which is why the filter edges (+50, +3000) matter.
        audio = iq_to_usb_audio(_tone(-1500.0, amp=0.25))
        self.assertAlmostEqual(_amp_at(audio, 1500.0), 0.25, places=5)

    def test_audio_is_the_real_part_exactly(self):
        iq = (np.arange(8) + 1j * np.arange(8)[::-1]).astype(np.complex64)
        np.testing.assert_array_equal(iq_to_usb_audio(iq), iq.real)

    def test_non_finite_pass_through(self):
        iq = np.array([1 + 1j, np.nan + 0j, complex(0.0, np.inf)], dtype=np.complex64)
        audio = iq_to_usb_audio(iq)
        self.assertTrue(np.isnan(audio[1]))
        self.assertEqual(audio[2], 0.0)       # the imaginary inf does not reach the real part


class IqWavTests(unittest.TestCase):
    def test_iq_wav_roundtrip(self):
        rng = np.random.default_rng(1)
        iq = (rng.standard_normal(SR) + 1j * rng.standard_normal(SR)).astype(np.complex64)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.iq.wav"
            write_iq_wav(p, iq, SR)
            d = p.read_bytes()
        self.assertEqual(d[:4], b"RIFF")
        tag, ch, sr, br, align, bits = struct.unpack_from("<HHIIHH", d, 20)
        self.assertEqual((tag, ch, sr, br, align, bits), (3, 2, SR, SR * 8, 8, 32))
        data_at = d.index(b"data") + 8
        size = struct.unpack_from("<I", d, data_at - 4)[0]
        self.assertEqual(size, SR * 8)
        self.assertEqual(struct.unpack_from("<I", d, 4)[0], len(d) - 8)
        x = np.frombuffer(d, dtype="<f4", offset=data_at).reshape(-1, 2)
        np.testing.assert_array_equal(x[:, 0], iq.real)       # channel 1 = I, exact
        np.testing.assert_array_equal(x[:, 1], iq.imag)       # channel 2 = Q, exact

    def test_iq_wav_keeps_non_finite_as_received(self):
        iq = np.array([1 + 2j, np.nan + 1j, 3 + np.inf * 1j], dtype=np.complex64)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.iq.wav"
            write_iq_wav(p, iq, SR)
            d = p.read_bytes()
        x = np.frombuffer(d, dtype="<f4", offset=d.index(b"data") + 8).reshape(-1, 2)
        self.assertTrue(np.isnan(x[1, 0]))
        self.assertTrue(np.isinf(x[2, 1]))
        self.assertEqual(x[0, 0], 1.0)


class IqRingTests(unittest.TestCase):
    def test_iq_gap_zero_filled(self):
        # A 2 s gap inside a 30 s slot (tr_period_sec = 30): 2 s of 0+0j, and the count exact.
        ring = Ring(max_seconds=60, sample_rate=SR, dtype=np.complex64)
        a = np.full(14 * SR, 1 + 1j, dtype=np.complex64)
        b = np.full(14 * SR, 2 - 1j, dtype=np.complex64)
        ring.push(a, 0)                            # seconds 0..14
        ring.push(b, 16 * SR)                      # seconds 14..16 never arrive; 16..30
        out = ring.extract_by_offset(0, 30 * SR)
        self.assertEqual(out.dtype, np.complex64)
        self.assertEqual(len(out), 30 * SR)
        np.testing.assert_array_equal(out[14 * SR:16 * SR], np.zeros(2 * SR, dtype=np.complex64))
        np.testing.assert_array_equal(out[:14 * SR], a)
        np.testing.assert_array_equal(out[16 * SR:], b)

    def test_iq_gap_in_a_15_s_slot_stays_exact(self):
        ring = Ring(max_seconds=60, sample_rate=SR, dtype=np.complex64)
        ring.push(np.full(5 * SR, 1 + 1j, dtype=np.complex64), 0)
        ring.push(np.full(9 * SR, 1 + 1j, dtype=np.complex64), 6 * SR)   # 1 s lost; 5..6
        out = ring.extract_by_offset(0, 15 * SR)                          # 14 of 15 s: kept
        self.assertEqual(len(out), 15 * SR)
        self.assertEqual(np.count_nonzero(out[5 * SR:6 * SR]), 0)

    def test_default_ring_stays_float32(self):
        ring = Ring(max_seconds=30, sample_rate=SR)
        ring.push(np.ones(SR, dtype=np.float32), 0)
        self.assertEqual(ring.extract_by_offset(0, SR).dtype, np.float32)


class IqWriteAtomicTests(unittest.TestCase):
    def test_success_leaves_no_part_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.iq.wav"
            write_iq_wav(p, np.ones(10, dtype=np.complex64), SR)
            self.assertEqual(sorted(f.name for f in Path(td).iterdir()), ["x.iq.wav"])

    def test_failed_write_leaves_nothing(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.iq.wav"
            with mock.patch("meteor_scatter.core.iq._commit", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_iq_wav(p, np.ones(10, dtype=np.complex64), SR)
            self.assertEqual(list(Path(td).iterdir()), [])        # no file, no .part

    def test_a_reader_never_sees_a_half_written_file(self):
        # The data lands in <name>.part; the final name appears only at the rename.
        from unittest import mock
        seen = []
        real = os_replace = __import__("os").replace

        def spy(src, dst):
            seen.append((Path(src).name, Path(dst).exists()))
            return real(src, dst)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("os.replace", side_effect=spy):
                write_iq_wav(Path(td) / "x.iq.wav", np.ones(10, dtype=np.complex64), SR)
        self.assertEqual(seen, [("x.iq.wav.part", False)])

    def test_frequency_xattr_is_carried(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe"
            probe.write_bytes(b"")
            try:
                os.setxattr(str(probe), "user.t", b"1")
            except OSError:
                self.skipTest("filesystem has no user xattrs")
            p = Path(td) / "x.iq.wav"
            write_iq_wav(p, np.ones(10, dtype=np.complex64), SR, frequency_hz=50260000)
            self.assertEqual(os.getxattr(str(p), "user.frequency"), b"50260000")


class RingDtypeFaultTests(unittest.TestCase):
    def test_audio_into_an_iq_ring_is_refused_not_cast(self):
        ring = Ring(max_seconds=30, sample_rate=SR, dtype=np.complex64)
        with self.assertLogs("meteor_scatter.core.ring", "ERROR") as cm:
            for i in range(5):
                ring.push(np.ones(SR, dtype=np.float32), i * SR)
        self.assertEqual(len(cm.records), 1)                      # once, not per batch
        self.assertEqual(ring.total_samples, 0)                   # nothing stored
        self.assertIsNone(ring.head_offset())
        self.assertEqual(ring.dtype_faults, 5)

    def test_complex_into_an_audio_ring_is_refused_too(self):
        ring = Ring(max_seconds=30, sample_rate=SR)
        with self.assertLogs("meteor_scatter.core.ring", "ERROR"):
            ring.push(np.ones(SR, dtype=np.complex64), 0)
        self.assertEqual(ring.total_samples, 0)

    def test_fault_clears_with_one_info_line(self):
        ring = Ring(max_seconds=30, sample_rate=SR, dtype=np.complex64)
        with self.assertLogs("meteor_scatter.core.ring", "INFO") as cm:
            ring.push(np.ones(SR, dtype=np.float32), 0)
            ring.push(np.ones(SR, dtype=np.complex64), SR)
            ring.push(np.ones(SR, dtype=np.complex64), 2 * SR)
        self.assertEqual([r.levelname for r in cm.records], ["ERROR", "INFO"])
        self.assertEqual(ring.total_samples, 2 * SR)


if __name__ == "__main__":
    unittest.main()
