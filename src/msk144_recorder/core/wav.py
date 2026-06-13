"""Minimal WAV writer for decode_ft8 input.

Writes standard RIFF WAV: mono, 16-bit signed PCM, little-endian.
This matches what jt-decoded/pcmrecord produces for decode_ft8.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    frequency_hz: int = 0,
) -> None:
    """Write float32 samples as a 16-bit PCM WAV file.

    Args:
        path: Output WAV file path.
        samples: float32 audio samples, normalized [-1, 1].
        sample_rate: Sample rate in Hz (e.g. 12000).
        frequency_hz: Optional center frequency for xattr metadata.
    """
    int16_samples = _float32_to_int16(samples)
    data_bytes = int16_samples.tobytes()
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


# Peak-normalize to -1 dBFS, matching wspr-recorder's wav_writer
# (per-slot peak -> full int16 range).  An earlier per-slot peak-norm
# here (789064f, reverted 50bd7d9) was abandoned in the s16-channel era:
# one transient set the peak and scaled the already-quantized signal
# into the noise floor.  With f32 channels the full dynamic range
# survives to this point, so peak-norm uses the whole int16 range for
# low-level signals (e.g. a 25 dB-down FT8 channel) instead of letting
# them quantize into the floor.
_PEAK_TARGET_INT16 = 32767.0 * (10.0 ** (-1.0 / 20.0))   # -1 dBFS ~= 29205


def _float32_to_int16(samples: np.ndarray) -> np.ndarray:
    """Peak-normalize float32 audio to int16 at -1 dBFS.

    REQUIRES f32 channels on the wire: an s16 channel is already
    quantized at the radiod before we see it, so this cannot recover
    what s16 has thrown away.
    """
    if samples.size == 0:
        return np.zeros(0, dtype=np.int16)
    peak = float(np.abs(samples).max())
    if peak <= 0.0:
        return np.zeros(samples.size, dtype=np.int16)
    scaled = samples * (_PEAK_TARGET_INT16 / peak)
    return np.clip(scaled, -32768.0, 32767.0).astype(np.int16)


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
