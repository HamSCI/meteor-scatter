"""Process-local ring buffer for MSK144 sample accumulation.

Keyed by **absolute RTP sample offset** (an unwrapped 64-bit sample count
measured from the SlotClock anchor), NOT by floating-point UTC.  Each batch
is tagged with the offset of its first sample, derived from the GPS-true RTP
timestamp radiod stamps on the packet (see ChannelSink.on_samples).  Slot
extraction then asks for an exact ``[start_off, start_off+n)`` window, so the
audio handed to jt9 always corresponds to the RTP grid point its WAV is
labelled with — immune to the delivered-sample-count drift that the old
UTC-projection ring suffered (real RF, wrong label -> 0 decodes).

Simple deque of (samples, start_offset) tuples behind a threading.Lock.
No SysV IPC, no cross-process consumers.  Sized to hold ~3 cadences.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class Ring:
    """Accumulates float32 audio samples keyed by absolute RTP sample offset."""

    def __init__(self, max_seconds: float, sample_rate: int):
        self._lock = threading.Lock()
        self._sample_rate = sample_rate
        self._max_samples = int(max_seconds * sample_rate)
        # (samples, start_offset) — start_offset is the absolute RTP sample
        # offset (from the SlotClock anchor) of this chunk's first sample.
        self._chunks: deque[tuple[np.ndarray, int]] = deque()
        self._total_samples = 0

    def push(self, samples: np.ndarray, start_offset: int) -> None:
        """Append a batch tagged with the absolute offset of its first sample."""
        n = len(samples)
        if n == 0:
            return
        with self._lock:
            self._chunks.append((samples, int(start_offset)))
            self._total_samples += n
            while self._total_samples > self._max_samples and self._chunks:
                dropped, _ = self._chunks.popleft()
                self._total_samples -= len(dropped)

    def clear(self) -> None:
        """Drop all buffered samples (used on SlotClock re-anchor)."""
        with self._lock:
            self._chunks.clear()
            self._total_samples = 0

    def head_offset(self) -> Optional[int]:
        """Absolute offset just past the most recent sample, or None if empty."""
        with self._lock:
            if not self._chunks:
                return None
            last_samples, last_off = self._chunks[-1]
            return last_off + len(last_samples)

    def tail_offset(self) -> Optional[int]:
        """Absolute offset of the oldest sample still resident, or None."""
        with self._lock:
            if not self._chunks:
                return None
            _, first_off = self._chunks[0]
            return first_off

    def extract_by_offset(
        self, start_offset: int, n_samples: int
    ) -> Optional[np.ndarray]:
        """Extract exactly ``n_samples`` starting at absolute ``start_offset``.

        Returns a float32 array of length ``n_samples``, or None if the ring
        doesn't cover at least 90% of the requested interval.  Any small
        shortfall inside the window (an evicted head or a not-yet-arrived
        tail) is zero-padded so the decoder always gets a fixed-length slot.
        """
        end_offset = start_offset + n_samples
        with self._lock:
            if not self._chunks:
                return None
            pieces: list[tuple[int, np.ndarray]] = []   # (dest_index, samples)
            collected = 0
            for chunk_samples, chunk_off in self._chunks:
                chunk_end = chunk_off + len(chunk_samples)
                if chunk_end <= start_offset:
                    continue
                if chunk_off >= end_offset:
                    break
                src_start = max(0, start_offset - chunk_off)
                src_end = min(len(chunk_samples), end_offset - chunk_off)
                if src_start >= src_end:
                    continue
                dest_index = (chunk_off + src_start) - start_offset
                piece = chunk_samples[src_start:src_end]
                pieces.append((dest_index, piece))
                collected += len(piece)

        if collected < n_samples * 0.9:
            return None

        result = np.zeros(n_samples, dtype=np.float32)
        for dest_index, piece in pieces:
            result[dest_index:dest_index + len(piece)] = piece
        return result

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def total_samples(self) -> int:
        with self._lock:
            return self._total_samples
