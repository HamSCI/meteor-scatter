"""Tests for the process-local ring buffer."""

import unittest

import numpy as np

from meteor_scatter.core.ring import Ring


class RingBasicTests(unittest.TestCase):

    def test_empty_ring_head_is_none(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        self.assertIsNone(ring.head_utc())

    def test_push_and_head_utc(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        samples = np.zeros(12000, dtype=np.float32)
        ring.push(samples, utc_of_first_sample=1000.0)
        self.assertAlmostEqual(ring.head_utc(), 1001.0, places=3)

    def test_total_samples(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        ring.push(np.zeros(6000, dtype=np.float32), 1000.0)
        ring.push(np.zeros(6000, dtype=np.float32), 1000.5)
        self.assertEqual(ring.total_samples, 12000)

    def test_capacity_eviction(self):
        ring = Ring(max_seconds=2, sample_rate=12000)
        for i in range(5):
            ring.push(np.zeros(12000, dtype=np.float32), 1000.0 + i)
        self.assertLessEqual(ring.total_samples, 24000)


class RingExtractTests(unittest.TestCase):

    def _make_ring(self):
        """Create a ring with 20 seconds of data starting at t=1000."""
        ring = Ring(max_seconds=30, sample_rate=12000)
        for i in range(40):
            t = 1000.0 + i * 0.5
            samples = np.full(6000, fill_value=float(i % 10), dtype=np.float32)
            ring.push(samples, t)
        return ring

    def test_extract_aligned_slot(self):
        ring = self._make_ring()
        slot = ring.extract_slot(1000.0, 15.0)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 180000)

    def test_extract_short_slot(self):
        ring = self._make_ring()
        slot = ring.extract_slot(1000.0, 7.5)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 90000)

    def test_extract_returns_none_if_not_covered(self):
        ring = Ring(max_seconds=5, sample_rate=12000)
        ring.push(np.zeros(6000, dtype=np.float32), 1000.0)
        slot = ring.extract_slot(999.0, 15.0)
        self.assertIsNone(slot)

    def test_extract_across_chunk_boundary(self):
        ring = Ring(max_seconds=30, sample_rate=12000)
        ring.push(np.ones(60000, dtype=np.float32), 1000.0)
        ring.push(np.full(60000, 2.0, dtype=np.float32), 1005.0)
        slot = ring.extract_slot(1003.0, 4.0)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 48000)
        self.assertAlmostEqual(slot[0], 1.0)
        self.assertAlmostEqual(slot[-1], 2.0)


if __name__ == "__main__":
    unittest.main()
