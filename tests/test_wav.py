"""Tests for the WAV writer."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from meteor_scatter.core.wav import write_wav, write_wav_f32, quiet_floor_dbfs, WavStats


class WavWriterTests(unittest.TestCase):

    def test_write_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            samples = np.sin(
                np.linspace(0, 2 * np.pi * 440, 12000, dtype=np.float32)
            )
            write_wav(path, samples, sample_rate=12000)

            self.assertTrue(path.exists())
            data = path.read_bytes()

            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual(data[8:12], b"WAVE")
            self.assertEqual(data[12:16], b"fmt ")

            fmt_size = struct.unpack_from("<I", data, 16)[0]
            self.assertEqual(fmt_size, 16)

            audio_format = struct.unpack_from("<H", data, 20)[0]
            self.assertEqual(audio_format, 1)

            channels = struct.unpack_from("<H", data, 22)[0]
            self.assertEqual(channels, 1)

            sr = struct.unpack_from("<I", data, 24)[0]
            self.assertEqual(sr, 12000)

            bits = struct.unpack_from("<H", data, 34)[0]
            self.assertEqual(bits, 16)

    def test_correct_sample_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            n_samples = 180000
            samples = np.zeros(n_samples, dtype=np.float32)
            write_wav(path, samples, sample_rate=12000)

            data = path.read_bytes()
            data_size = struct.unpack_from("<I", data, 40)[0]
            self.assertEqual(data_size, n_samples * 2)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "test.wav"
            write_wav(path, np.zeros(100, dtype=np.float32), 12000)
            self.assertTrue(path.exists())

    def test_peak_normalization(self):
        """Verify peak-normalization to -1 dBFS (wav.py): the per-slot
        peak maps to ~29205 LSB regardless of input level, using the full
        int16 range for low-level signals.

        This superseded the earlier RMS-target normalization once f32
        channels preserved the full dynamic range to this point (see the
        _peak_norm_gain_db docstring in wav.py).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            # 1 kHz sine at amplitude 0.1 → peak 0.1 → normalized to -1 dBFS
            sr = 12000
            t = np.arange(sr, dtype=np.float32) / sr
            samples = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            write_wav(path, samples, sample_rate=sr)

            int16_vals = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            measured_peak = int(np.abs(int16_vals).max())
            # -1 dBFS target ≈ 29205 LSB, independent of input amplitude.
            self.assertAlmostEqual(measured_peak, 29205, delta=50)

    def test_clip_at_int16_bounds(self):
        """Even with the RMS-target gain, MAX_GAIN can let extreme
        samples exceed int16 range — the clip(±32768, ±32767) backstop
        in _to_int16 keeps the output in range.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            # Tiny-RMS input with one giant transient: the RMS-targeted
            # gain is huge, the transient hits the clip backstop.
            samples = np.zeros(1000, dtype=np.float32)
            samples[0] = 10.0
            samples[1] = -10.0
            write_wav(path, samples, sample_rate=12000)

            int16_vals = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            self.assertLessEqual(int16_vals.max(), 32767)
            self.assertGreaterEqual(int16_vals.min(), -32768)


if __name__ == "__main__":
    unittest.main()


class FixedScaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def _read_int16(self):
        return np.frombuffer(self.path.read_bytes()[44:], dtype="<i2")

    def test_fixed_scale_applies_exactly_and_reports_it(self):
        x = np.full(1200, 0.001, dtype=np.float32)
        st = write_wav(self.path, x, 12000, scale_db=20.0)
        self.assertEqual(st.gain_db, 20.0)
        self.assertEqual(int(self._read_int16()[0]), round(0.001 * 32767 * 10.0))
        self.assertEqual(st.clipped_samples, 0)

    def test_fixed_scale_counts_clipped_samples(self):
        x = np.array([0.5, -0.5, 0.01], dtype=np.float32)
        st = write_wav(self.path, x, 12000, scale_db=12.0)   # 0.5 * 3.98 > 1
        self.assertEqual(st.clipped_samples, 2)
        self.assertEqual(int(self._read_int16()[0]), 32767)
        self.assertEqual(int(self._read_int16()[1]), -32768)

    def test_peak_norm_reports_its_gain(self):
        x = np.array([0.0, 0.25, -0.1], dtype=np.float32)
        st = write_wav(self.path, x, 12000)
        expected = 20 * np.log10((32767.0 * 10 ** (-1 / 20) / 0.25) / 32767.0)
        self.assertAlmostEqual(st.gain_db, expected, places=6)
        self.assertEqual(st.clipped_samples, 0)

    def test_quiet_floor_ignores_a_loud_ping(self):
        rng = np.random.default_rng(1)
        x = (rng.standard_normal(12000 * 30) * 1e-3).astype(np.float32)
        x[12000:14400] += 0.3                      # a 200 ms ping
        floor = quiet_floor_dbfs(x, 12000)
        self.assertAlmostEqual(floor, -60.0, delta=1.5)

    def test_quiet_floor_of_silence_is_none(self):
        self.assertIsNone(quiet_floor_dbfs(np.zeros(12000, np.float32), 12000))


class F32WavTests(unittest.TestCase):

    def _sine(self, dbfs, n=12000 * 15):
        t = np.arange(n) / 12000.0
        return (10.0 ** (dbfs / 20.0) * np.sin(2 * np.pi * 1500.0 * t)).astype(np.float32)

    def test_f32_wav_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "f.wav"
            write_wav_f32(path, self._sine(-20, 12000), 12000)
            d = path.read_bytes()
            self.assertEqual(d[:4], b"RIFF")
            self.assertEqual(d[8:16], b"WAVEfmt ")
            fmt_size, tag, ch, sr, byte_rate, align, bits = struct.unpack_from("<IHHIIHH", d, 16)
            self.assertEqual((tag, ch, sr, bits), (3, 1, 12000, 32))
            self.assertEqual((byte_rate, align), (12000 * 4, 4))
            self.assertEqual(d[16 + 4 + fmt_size:16 + 8 + fmt_size], b"data")
            data_size = struct.unpack_from("<I", d, 20 + fmt_size + 4)[0]
            self.assertEqual(data_size, 12000 * 4)
            self.assertEqual(len(d), 8 + 4 + 8 + fmt_size + 8 + data_size)
            self.assertEqual(struct.unpack_from("<I", d, 4)[0], len(d) - 8)

    def test_f32_audio_keeps_gain(self):
        # -135 dBFS: 16-bit rounding would destroy it; F32 keeps it, scaled by the same gain.
        x = self._sine(-135)
        with tempfile.TemporaryDirectory() as tmpdir:
            p16 = Path(tmpdir) / "s.wav"
            p32 = Path(tmpdir) / "f.wav"
            st16 = write_wav(p16, x, 12000)
            st32 = write_wav_f32(p32, x, 12000)
            self.assertEqual(st32.gain_db, st16.gain_db)
            self.assertEqual(st32.clipped_samples, 0)
            self.assertEqual(st32.noise_dbfs, st16.noise_dbfs)
            d = p32.read_bytes()
            got = np.frombuffer(d[-x.size * 4:], dtype="<f4")
            want = (x.astype(np.float64) * 10.0 ** (st16.gain_db / 20.0)).astype(np.float32)
            np.testing.assert_array_equal(got, want)

    def test_f32_fixed_gain_does_not_clip(self):
        x = np.full(100, 0.5, dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "f.wav"
            st = write_wav_f32(path, x, 12000, scale_db=12.0)
            got = np.frombuffer(path.read_bytes()[-400:], dtype="<f4")
            self.assertGreater(float(got[0]), 1.0)
            self.assertEqual(st.clipped_samples, 0)
            self.assertEqual(st.gain_db, 12.0)
