"""Process-local ring buffer for FT4/FT8 sample accumulation.

Simple deque of (samples, utc_start) tuples behind a threading.Lock.
No SysV IPC, no cross-process consumers. Sized to hold ~3 cadences
(~45 seconds at 12 kHz = ~540 KB).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class Ring:
    """Accumulates float32 audio samples with UTC timing."""

    def __init__(self, max_seconds: float, sample_rate: int):
        self._lock = threading.Lock()
        self._sample_rate = sample_rate
        self._max_samples = int(max_seconds * sample_rate)
        self._chunks: deque[tuple[np.ndarray, float]] = deque()
        self._total_samples = 0
        self._anchor_utc: Optional[float] = None
        self._anchor_sample_offset: int = 0
        self._cumulative_samples: int = 0

    def push(self, samples: np.ndarray, utc_of_first_sample: float) -> None:
        """Append a batch of samples with their UTC timestamp."""
        n = len(samples)
        if n == 0:
            return
        with self._lock:
            if self._anchor_utc is None:
                self._anchor_utc = utc_of_first_sample
                self._anchor_sample_offset = 0
            self._chunks.append((samples, utc_of_first_sample))
            self._total_samples += n
            self._cumulative_samples += n
            while self._total_samples > self._max_samples and self._chunks:
                dropped, _ = self._chunks.popleft()
                self._total_samples -= len(dropped)

    def head_utc(self) -> Optional[float]:
        """UTC of the most recent sample, or None if empty."""
        with self._lock:
            if not self._chunks:
                return None
            last_samples, last_utc = self._chunks[-1]
            return last_utc + len(last_samples) / self._sample_rate

    def extract_slot(
        self, slot_start_utc: float, duration_sec: float
    ) -> Optional[np.ndarray]:
        """Extract exactly duration_sec of samples starting at slot_start_utc.

        Returns a float32 array of length int(duration_sec * sample_rate),
        or None if the ring doesn't cover the requested interval.
        """
        target_samples = int(duration_sec * self._sample_rate)
        slot_end_utc = slot_start_utc + duration_sec

        with self._lock:
            if not self._chunks:
                return None

            pieces: list[np.ndarray] = []
            collected = 0

            for chunk_samples, chunk_utc in self._chunks:
                chunk_end_utc = chunk_utc + len(chunk_samples) / self._sample_rate

                if chunk_end_utc <= slot_start_utc:
                    continue
                if chunk_utc >= slot_end_utc:
                    break

                start_offset = max(
                    0,
                    int((slot_start_utc - chunk_utc) * self._sample_rate),
                )
                end_offset = min(
                    len(chunk_samples),
                    int((slot_end_utc - chunk_utc) * self._sample_rate),
                )
                if start_offset >= end_offset:
                    continue

                piece = chunk_samples[start_offset:end_offset]
                pieces.append(piece)
                collected += len(piece)

        if collected < target_samples * 0.9:
            return None

        result = np.concatenate(pieces) if pieces else np.array([], dtype=np.float32)
        if len(result) > target_samples:
            result = result[:target_samples]
        elif len(result) < target_samples:
            result = np.pad(result, (0, target_samples - len(result)))
        return result

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def total_samples(self) -> int:
        with self._lock:
            return self._total_samples
