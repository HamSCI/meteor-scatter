# Copy of hf-timestd's src/hf_timestd/core/iq_codec.py, which is the source of truth.
# meteor-scatter does not depend on hf-timestd, so it carries its own copy.
# tests/test_archive_f16.py pins this copy to the bytes hf-timestd produces.
"""F16 (IEEE binary16) codec for 24 kHz complex I/Q archives.

An archive written as F16 stores each real and imaginary component as
``binary16(component * F16_SCALE)``.  The scale is a code constant, so a
sidecar only needs the dtype string to decode.  Samples whose scaled
magnitude would overflow binary16 cannot be stored as F16; ``encode_f16``
returns None for them and the caller falls back to complex64.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

F16_SCALE: float = 4096.0  # 2**12, fixed in code
DTYPE_F32 = "complex64"
DTYPE_F16 = "complex-f16"

_F16_MAX = 65504.0  # largest finite binary16 value


def encode_f16(samples: np.ndarray) -> Optional[bytes]:
    """Encode complex samples as little-endian interleaved binary16 bytes.

    Returns None when any finite scaled component exceeds the binary16 range.
    NaN and inf pass through unchanged and never trigger the fallback.
    """
    interleaved = np.ascontiguousarray(samples, dtype=np.complex64).view(np.float32)
    scaled = interleaved * np.float32(F16_SCALE)
    finite = np.isfinite(scaled)
    if np.any(np.abs(scaled[finite]) > _F16_MAX):
        return None
    return scaled.astype("<f2").tobytes()


def decode_iq(raw: bytes, dtype: str, scale: float = 1.0) -> np.ndarray:
    """Decode raw archive bytes to complex64, dividing out ``scale``."""
    if dtype == DTYPE_F32:
        components = np.frombuffer(raw, dtype=np.float32)
    elif dtype == DTYPE_F16:
        components = np.frombuffer(raw, dtype="<f2")
    else:
        raise ValueError(f"unknown I/Q dtype: {dtype!r}")
    values = components.astype(np.float32) / np.float32(scale)
    return values.view(np.complex64)


def bytes_per_sample(dtype: str) -> int:
    """Bytes per complex sample for an I/Q dtype string."""
    if dtype == DTYPE_F32:
        return 8
    if dtype == DTYPE_F16:
        return 4
    raise ValueError(f"unknown I/Q dtype: {dtype!r}")
