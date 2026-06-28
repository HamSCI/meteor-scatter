"""Tests for the process-local ring buffer (RTP-sample-offset keyed)."""

import unittest

import numpy as np

from meteor_scatter.core.ring import Ring


class RingBasicTests(unittest.TestCase):

    def test_empty_ring_head_is_none(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        self.assertIsNone(ring.head_offset())
        self.assertIsNone(ring.tail_offset())

    def test_push_and_head_offset(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        ring.push(np.zeros(12000, dtype=np.float32), start_offset=0)
        self.assertEqual(ring.head_offset(), 12000)
        self.assertEqual(ring.tail_offset(), 0)

    def test_total_samples(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        ring.push(np.zeros(6000, dtype=np.float32), 0)
        ring.push(np.zeros(6000, dtype=np.float32), 6000)
        self.assertEqual(ring.total_samples, 12000)

    def test_capacity_eviction(self):
        ring = Ring(max_seconds=2, sample_rate=12000)
        for i in range(5):
            ring.push(np.zeros(12000, dtype=np.float32), i * 12000)
        self.assertLessEqual(ring.total_samples, 24000)

    def test_clear(self):
        ring = Ring(max_seconds=10, sample_rate=12000)
        ring.push(np.zeros(6000, dtype=np.float32), 0)
        ring.clear()
        self.assertEqual(ring.total_samples, 0)
        self.assertIsNone(ring.head_offset())


class RingExtractTests(unittest.TestCase):

    def _make_ring(self):
        """20 s of data as 0.5 s chunks starting at offset 0; chunk i is
        filled with the value (i % 10) so extraction content is checkable."""
        ring = Ring(max_seconds=30, sample_rate=12000)
        for i in range(40):
            samples = np.full(6000, fill_value=float(i % 10), dtype=np.float32)
            ring.push(samples, start_offset=i * 6000)
        return ring

    def test_extract_aligned_slot(self):
        ring = self._make_ring()
        slot = ring.extract_by_offset(0, 180000)   # 15 s (MSK144 cadence)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 180000)

    def test_extract_short_slot(self):
        ring = self._make_ring()
        slot = ring.extract_by_offset(0, 90000)    # 7.5 s
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 90000)

    def test_extract_returns_none_if_not_covered(self):
        ring = Ring(max_seconds=5, sample_rate=12000)
        ring.push(np.zeros(6000, dtype=np.float32), 0)
        # ask for a 15 s window the ring can't cover -> None
        self.assertIsNone(ring.extract_by_offset(0, 180000))

    def test_extract_across_chunk_boundary(self):
        ring = Ring(max_seconds=30, sample_rate=12000)
        ring.push(np.ones(60000, dtype=np.float32), start_offset=0)
        ring.push(np.full(60000, 2.0, dtype=np.float32), start_offset=60000)
        # window straddling the boundary: [36000, 84000) = 48000 samples
        slot = ring.extract_by_offset(36000, 48000)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 48000)
        self.assertAlmostEqual(slot[0], 1.0)
        self.assertAlmostEqual(slot[-1], 2.0)

    def test_extract_zero_pads_small_tail_gap(self):
        # ring covers [0, 180000) but we ask for [6000, 186000): 174000 of
        # 180000 present (>90%) -> returned, tail zero-padded.
        ring = Ring(max_seconds=30, sample_rate=12000)
        ring.push(np.ones(180000, dtype=np.float32), start_offset=0)
        slot = ring.extract_by_offset(6000, 180000)
        self.assertIsNotNone(slot)
        self.assertEqual(len(slot), 180000)
        self.assertAlmostEqual(slot[0], 1.0)
        self.assertAlmostEqual(slot[-1], 0.0)   # zero-padded tail


if __name__ == "__main__":
    unittest.main()
