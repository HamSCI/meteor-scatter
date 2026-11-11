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
_BLOCK = 1 << 20  # float32 components scaled and checked per step (4 MB)


def encode_f16(samples: np.ndarray) -> Optional[bytes]:
    """Encode complex samples as little-endian interleaved binary16 bytes.

    Returns None when any finite scaled component exceeds the binary16 range.
    NaN and inf pass through unchanged and never trigger the fallback.

    Works in blocks of ``_BLOCK`` components into one preallocated binary16
    array, so a 600 s chunk needs the output plus a few MB, not several
    full-size float32 temporaries.  The bytes equal
    ``(x * F16_SCALE).astype("<f2")`` on the interleaved float32 view.
    """
    interleaved = np.ascontiguousarray(samples, dtype=np.complex64).view(np.float32).reshape(-1)
    out = np.empty(interleaved.size, dtype="<f2")
    scale = np.float32(F16_SCALE)
    scaled_buf = np.empty(min(_BLOCK, interleaved.size), dtype=np.float32)
    for start in range(0, interleaved.size, _BLOCK):
        block = interleaved[start:start + _BLOCK]
        scaled = scaled_buf[:block.size]
        np.multiply(block, scale, out=scaled)
        big = np.abs(scaled) > _F16_MAX  # NaN compares False; inf compares True
        if big.any() and np.isfinite(scaled[big]).any():
            return None
        out[start:start + block.size] = scaled
    return out.tobytes()


def decode_iq(raw: bytes, dtype: str, scale: float = 1.0) -> np.ndarray:
    """Decode raw archive bytes to complex64, dividing out ``scale``.

    complex64 at scale 1.0 comes back as a zero-copy, read-only view of
    ``raw``; every other case returns a fresh, writable array.
    """
    if dtype == DTYPE_F32:
        components = np.frombuffer(raw, dtype=np.float32)
        if scale == 1.0:
            return components.view(np.complex64)
    elif dtype == DTYPE_F16:
        components = np.frombuffer(raw, dtype="<f2")
    else:
        raise ValueError(f"unknown I/Q dtype: {dtype!r}")
    values = components.astype(np.float32)
    values /= np.float32(scale)
    return values.view(np.complex64)


def bytes_per_sample(dtype: str) -> int:
    """Bytes per complex sample for an I/Q dtype string."""
    if dtype == DTYPE_F32:
        return 8
    if dtype == DTYPE_F16:
        return 4
    raise ValueError(f"unknown I/Q dtype: {dtype!r}")
