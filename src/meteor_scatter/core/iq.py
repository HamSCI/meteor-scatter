"""The I/Q channel: the USB audio made from it, and the file that keeps it.

With capture = "iq", radiod runs its `iq` preset at 12 kHz complex, F32, AGC off, gain 0 dB, with
the filter edges below: the `usb` preset's passband.  The filter leaves only the upper sideband, so
the I/Q is one-sided and its real part is the USB audio.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

from meteor_scatter.config import IQ_HIGH_EDGE_HZ, IQ_LOW_EDGE_HZ  # noqa: F401  (re-exported)
from meteor_scatter.core.wav import _set_xattrs

# Gain from the one-sided I/Q to the audio a USB demodulator gives.  radiod's mono linear
# demodulator outputs gain * Re(iq) and its `iq` preset outputs gain * iq with the same gain
# (ka9q-radio src/linear.c, the "I channel only" and "simplest case" branches), so at the same
# channel gain the audio is exactly Re(iq): a one-sided tone of amplitude A gives a real tone of
# amplitude A.  A factor of 2 would put the audio +6.02 dB above radiod's own USB audio, and every
# calibrated noise floor and scale_db measured on the usb path would shift by as much.
USB_AUDIO_GAIN = 1.0


def iq_to_usb_audio(iq: np.ndarray) -> np.ndarray:
    """Real float32 USB audio at the I/Q's sample rate.

    The channel's filter already makes the I/Q one-sided (+50 to +3000 Hz), so the real part is
    the audio.  NaN and inf pass through: the WAV writers deal with them.
    """
    return (USB_AUDIO_GAIN * iq.real).astype(np.float32)


def _commit(part: Path, path: Path) -> None:
    os.replace(part, path)


def write_iq_wav(path: Path, iq: np.ndarray, sample_rate: int, frequency_hz: int = 0) -> None:
    """Write I/Q as a two-channel 32-bit float WAV: channel 1 = I, channel 2 = Q.

    The samples go to disk exactly as received: no gain, no normalisation, and any NaN or inf
    stays, because this file is the record.  Header: IEEE float (tag 3), 18-byte fmt chunk with
    cbSize 0, like write_wav_f32.  ``frequency_hz`` set: the same ``user.frequency`` xattr the
    audio writers put on a recording.

    The file appears under ``path`` only whole: it is written as ``<path>.part`` and renamed.  On
    any failure the part file is removed, nothing is left at ``path``, and the error propagates.
    """
    data = np.ascontiguousarray(iq, dtype="<c8").view("<f4").tobytes()   # I0 Q0 I1 Q1 ...
    size = len(data)
    header = struct.pack(
        "<4sI4s"      # RIFF header
        "4sIHHIIHHH"  # fmt chunk (18 bytes, cbSize 0)
        "4sI",        # data chunk header
        b"RIFF",
        38 + size,
        b"WAVE",
        b"fmt ",
        18,
        3,                 # IEEE float
        2,                 # I, Q
        int(sample_rate),
        int(sample_rate) * 8,
        8,
        32,
        0,
        b"data",
        size,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with open(part, "wb") as f:
            f.write(header)
            f.write(data)
        if frequency_hz:
            _set_xattrs(part, sample_rate, frequency_hz)
        _commit(part, path)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def wav_layout(buf: bytes) -> tuple[tuple, int, int]:
    """((tag, channels, rate, byte_rate, align, bits), data offset, data size) from the start of a
    WAV, found by walking its chunks.  ``buf`` needs to reach the data chunk's header only.
    ValueError when it is no WAV or the fmt chunk does not come before the data chunk."""
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt = 12, None
    while pos + 8 <= len(buf):
        cid, size = buf[pos:pos + 4], struct.unpack_from("<I", buf, pos + 4)[0]
        if cid == b"fmt ":
            if size < 16 or pos + 8 + 16 > len(buf):
                raise ValueError("fmt chunk is short")
            fmt = struct.unpack_from("<HHIIHH", buf, pos + 8)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV has no fmt chunk before its data")
            return fmt, pos + 8, size
        pos += 8 + size + (size & 1)
    raise ValueError("WAV lacks a fmt or data chunk")


def is_iq_format(fmt: tuple) -> bool:
    """True for IEEE float (tag 3), two channels, 32 bits: the I/Q file's format."""
    return (fmt[0], fmt[1], fmt[5]) == (3, 2, 32)


def read_iq_bytes(raw: bytes) -> tuple[np.ndarray, int]:
    """(complex64 I/Q, sample rate) from the bytes of a file write_iq_wav made.

    ValueError when the bytes are not a two-channel 32-bit float WAV.
    """
    fmt, off, size = wav_layout(raw)
    if not is_iq_format(fmt):
        raise ValueError(f"not two-channel 32-bit float I/Q (format {fmt[0]}, {fmt[1]} "
                         f"channel(s), {fmt[5]} bit)")
    n = min(size, len(raw) - off) // 8
    flat = np.frombuffer(raw, dtype="<f4", count=2 * n, offset=off)
    return np.ascontiguousarray(flat).view("<c8").astype(np.complex64), int(fmt[2])


def read_iq_wav(path: Path) -> tuple[np.ndarray, int]:
    """read_iq_bytes of a file."""
    return read_iq_bytes(Path(path).read_bytes())
