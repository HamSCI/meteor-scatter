"""The I/Q channel: the USB audio made from it, and the file that keeps it.

With capture = "iq", radiod runs its `iq` preset at 12 kHz complex, F32, AGC off, gain 0 dB, with
the filter edges below: the `usb` preset's passband.  The filter leaves only the upper sideband, so
the I/Q is one-sided and its real part is the USB audio.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from meteor_scatter.config import IQ_HIGH_EDGE_HZ, IQ_LOW_EDGE_HZ  # noqa: F401  (re-exported)

# Gain from the one-sided I/Q to the audio a USB demodulator gives.  radiod's mono linear
# demodulator outputs gain * Re(iq) and its `iq` preset outputs gain * iq with the same gain
# (ka9q-radio src/linear.c, the "I channel only" and "simplest case" branches), so at the same
# channel gain the audio is exactly Re(iq): a one-sided tone of amplitude A gives a real tone of
# amplitude A.  Task 3's brief said 2.0; with that the audio runs +6.02 dB above radiod's own USB
# audio, and every calibrated noise floor and scale_db measured on the usb path shifts by as much.
USB_AUDIO_GAIN = 1.0


def iq_to_usb_audio(iq: np.ndarray) -> np.ndarray:
    """Real float32 USB audio at the I/Q's sample rate.

    The channel's filter already makes the I/Q one-sided (+50 to +3000 Hz), so the real part is
    the audio.  NaN and inf pass through: the WAV writers deal with them.
    """
    return (USB_AUDIO_GAIN * iq.real).astype(np.float32)


def write_iq_wav(path: Path, iq: np.ndarray, sample_rate: int) -> None:
    """Write I/Q as a two-channel 32-bit float WAV: channel 1 = I, channel 2 = Q.

    The samples go to disk exactly as received: no gain, no normalisation, and any NaN or inf
    stays, because this file is the record.  Header: IEEE float (tag 3), 18-byte fmt chunk with
    cbSize 0, like write_wav_f32.
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
    with open(path, "wb") as f:
        f.write(header)
        f.write(data)
