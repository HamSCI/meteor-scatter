"""Minimal WAV writer for decode_ft8 input.

Writes standard RIFF WAV: mono, 16-bit signed PCM, little-endian.
This matches what jt-decoded/pcmrecord produces for decode_ft8.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


_FULL_SCALE = 32767.0


@dataclass(frozen=True)
class WavStats:
    """What write_wav did to one slot.  ``gain_db``: int16 = sample x 32767 x 10^(gain_db/20)."""
    gain_db: float
    clipped_samples: int
    noise_dbfs: Optional[float]


def quiet_floor_dbfs(samples: np.ndarray, sample_rate: int) -> Optional[float]:
    """The slot's noise floor: 10th percentile of 100 ms RMS blocks, dB re 1.0.

    The low percentile keeps a ping of up to a few seconds from raising it.
    None for an empty or all-zero slot.
    """
    block = max(1, sample_rate // 10)
    n = samples.size // block
    if n == 0:
        return None
    rms = np.sqrt(np.mean(samples[: n * block].astype(np.float64).reshape(n, block) ** 2, axis=1))
    floor = float(np.percentile(rms, 10))
    if floor <= 0.0:
        return None
    return 20.0 * np.log10(floor)


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    frequency_hz: int = 0,
    scale_db: Optional[float] = None,
) -> WavStats:
    """Write float32 samples as 16-bit PCM and report the gain applied.

    ``scale_db`` set: one fixed gain (a calibrated band).  ``None``: peak-normalize the slot to
    -1 dBFS, as before calibration existed.
    """
    gain_db = scale_db if scale_db is not None else _peak_norm_gain_db(samples)
    int16_samples, clipped = _to_int16(samples, gain_db)
    data_bytes = int16_samples.astype("<i2").tobytes()
    data_size = len(data_bytes)

    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s"     # RIFF header
        "4sIHHIIHH"  # fmt chunk
        "4sI",       # data chunk header
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,                # PCM format
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(data_bytes)

    if frequency_hz:
        _set_xattrs(path, sample_rate, frequency_hz)

    return WavStats(gain_db=gain_db, clipped_samples=clipped,
                    noise_dbfs=quiet_floor_dbfs(samples, sample_rate))


# Peak-normalize to -1 dBFS, matching wspr-recorder's wav_writer
# (per-slot peak -> full int16 range).  An earlier per-slot peak-norm
# here (789064f, reverted 50bd7d9) was abandoned in the s16-channel era:
# one transient set the peak and scaled the already-quantized signal
# into the noise floor.  With f32 channels the full dynamic range
# survives to this point, so peak-norm uses the whole int16 range for
# low-level signals (e.g. a 25 dB-down FT8 channel) instead of letting
# them quantize into the floor.
_PEAK_TARGET_INT16 = _FULL_SCALE * (10.0 ** (-1.0 / 20.0))   # -1 dBFS ~= 29205


def _peak_norm_gain_db(samples: np.ndarray) -> float:
    """Gain that peak-normalizes the slot to -1 dBFS (0 dB for an empty or silent slot).

    REQUIRES f32 channels on the wire: an s16 channel is already
    quantized at the radiod before we see it, so this cannot recover
    what s16 has thrown away.
    """
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    if peak <= 0.0:
        return 0.0
    return 20.0 * np.log10((_PEAK_TARGET_INT16 / peak) / _FULL_SCALE)


def _to_int16(samples: np.ndarray, gain_db: float):
    scaled = samples.astype(np.float64) * (_FULL_SCALE * 10.0 ** (gain_db / 20.0))
    rounded = np.rint(scaled)
    clipped = int(np.count_nonzero((rounded > 32767.0) | (rounded < -32768.0)))
    return np.clip(rounded, -32768.0, 32767.0).astype(np.int16), clipped


def _set_xattrs(path: Path, sample_rate: int, frequency_hz: int) -> None:
    """Set xattrs matching pcmrecord/jt-decoded conventions.

    These are optional — decode_ft8 works without them — but they
    let downstream tools identify the source without parsing filenames.
    """
    try:
        import os
        os.setxattr(str(path), "user.frequency", str(frequency_hz).encode())
    except (OSError, AttributeError):
        pass
