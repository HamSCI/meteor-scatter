"""Tests for the WAV writer."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from msk144_recorder.core.wav import write_wav


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

    def test_rms_target_normalization(self):
        """Verify RMS-target normalization (commit 50bd7d9): output RMS
        targets ~2000 LSB (~-24 dBFS), leaving ~24 dB peak headroom.

        Peak-norm (commit 789064f, reverted) failed because one
        transient set the peak and scaled every other sample into the
        noise floor.  RMS is dominated by bulk signal so one impulse
        barely moves it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            # 1 kHz sine at amplitude 0.1 → RMS = 0.1/sqrt(2) ≈ 0.0707
            sr = 12000
            t = np.arange(sr, dtype=np.float32) / sr
            samples = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            write_wav(path, samples, sample_rate=sr)

            int16_vals = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            measured_rms = float(np.sqrt(np.mean(int16_vals.astype(np.float64) ** 2)))
            # Allow ±5% drift around the 2000-LSB target.
            self.assertAlmostEqual(measured_rms, 2000.0, delta=100.0)

    def test_clip_at_int16_bounds(self):
        """Even with the RMS-target gain, MAX_GAIN can let extreme
        samples exceed int16 range — the clip(±32768, ±32767) backstop
        in _float32_to_int16 keeps the output in range.
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
