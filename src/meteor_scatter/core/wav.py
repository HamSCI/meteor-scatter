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
    """What write_wav (or write_wav_f32) did to one slot.

    ``gain_db``: int16 = sample x 32767 x 10^(gain_db/20); F32 = sample x 10^(gain_db/20).
    ``clipped_samples`` counts 16-bit clipping, so it is always 0 for F32.
    """
    gain_db: float
    clipped_samples: int
    noise_dbfs: Optional[float]
    # NaN or inf samples the slot held.  The writers replaced them with 0 in the audio file and
    # left them out of the peak and the noise floor.
    nonfinite_samples: int = 0


def quiet_floor_dbfs(samples: np.ndarray, sample_rate: int) -> Optional[float]:
    """The slot's noise floor: 10th percentile of 100 ms RMS blocks, dB re 1.0.

    The low percentile keeps a ping of up to a few seconds from raising it.
    None for an empty or all-zero slot.  NaN and inf samples count for nothing: each block's
    RMS runs over its finite samples, and a block with none drops out.
    """
    block = max(1, sample_rate // 10)
    n = samples.size // block
    if n == 0:
        return None
    x = samples[: n * block].astype(np.float64).reshape(n, block)
    finite = np.isfinite(x)
    counts = finite.sum(axis=1)
    sq = np.where(finite, x, 0.0) ** 2
    live = counts > 0
    if not live.any():
        return None
    rms = np.sqrt(sq.sum(axis=1)[live] / counts[live])
    floor = float(np.percentile(rms, 10))
    if floor <= 0.0:
        return None
    return 20.0 * np.log10(floor)


def sanitize(samples: np.ndarray) -> tuple[np.ndarray, int]:
    """(samples with NaN and inf replaced by 0, how many were replaced).

    Returns the input itself, uncopied, when every sample is finite.
    """
    finite = np.isfinite(samples)
    bad = int(samples.size - np.count_nonzero(finite))
    if bad == 0:
        return samples, 0
    return np.where(finite, samples, 0).astype(samples.dtype), bad


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
    clean, bad = sanitize(samples)
    gain_db = scale_db if scale_db is not None else _peak_norm_gain_db(clean)
    int16_samples, clipped = _to_int16(clean, gain_db)
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
                    noise_dbfs=quiet_floor_dbfs(samples, sample_rate), nonfinite_samples=bad)


def write_wav_f32(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    frequency_hz: int = 0,
    scale_db: Optional[float] = None,
) -> WavStats:
    """Write the slot as a mono 32-bit float WAV, with the gain write_wav would apply.

    Same gain choice as write_wav: ``scale_db``, or peak-normalize to -1 dBFS.  The samples
    are the float64 product ``samples x 10^(gain_db/20)`` cast to float32: no rounding to
    16 bits and no clipping, so ``clipped_samples`` is always 0.  Full scale is 1.0 (a
    16-bit sample s is s/32768), which is how UberSDR's jt9 reads float input.

    The 16-bit path scales by 32767 and this path does not, so for the same ``gain_db`` the
    two differ by 20*log10(32768/32767) = 0.0003 dB.  The peak-normalize target is the same
    -1 dBFS in both.

    NaN and inf samples become 0 in the file and count for nothing in the peak and the noise
    floor; ``WavStats.nonfinite_samples`` says how many there were.

    Format follows wspr-recorder's float WAV writer: IEEE float (tag 3), 18-byte fmt chunk
    with cbSize 0, 46-byte header.
    """
    clean, bad = sanitize(samples)
    gain_db = scale_db if scale_db is not None else _peak_norm_gain_db(clean)
    scaled = (clean.astype(np.float64) * 10.0 ** (gain_db / 20.0)).astype("<f4")
    data_bytes = scaled.tobytes()
    data_size = len(data_bytes)

    header = struct.pack(
        "<4sI4s"      # RIFF header
        "4sIHHIIHHH"  # fmt chunk (18 bytes, cbSize 0)
        "4sI",        # data chunk header
        b"RIFF",
        38 + data_size,
        b"WAVE",
        b"fmt ",
        18,
        3,                # IEEE float
        1,                # mono
        sample_rate,
        sample_rate * 4,
        4,
        32,
        0,
        b"data",
        data_size,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(data_bytes)

    if frequency_hz:
        _set_xattrs(path, sample_rate, frequency_hz)

    return WavStats(gain_db=gain_db, clipped_samples=0,
                    noise_dbfs=quiet_floor_dbfs(samples, sample_rate), nonfinite_samples=bad)


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
