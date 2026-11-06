"""What an MSQP submission holds (github.com/HamSCI/MSQP, Zenodo.md)."""
from __future__ import annotations

import html
from datetime import date, datetime

MSQP_FIELD_ORDER = [
    "Callsign Used (or PSKReporter Identifier)", "Operator Names and Calls", "Contact Email Address",
    "Six Character Grid Square", "Operation Mode", "Transceiver/Transmitter/Receiver Make(s) & Model(s)",
    "Transmitter Output Power", "Antenna", "Bands Operated", "Additional Comments",
]
EQUIPMENT = "RX888 MkII SDR with ka9q-radio radiod; decoded by WSJT-X jt9 (meteor-scatter)"


def grid6(grid: str) -> str:
    return grid.strip().upper()[:6]


def title(callsign: str, grid: str, first: date, last: date) -> str:
    if first == last:
        span = first.strftime("%Y%m%d")
    elif (first.year, first.month) == (last.year, last.month):
        span = f"{first.strftime('%Y%m%d')}-{last.strftime('%d')}"
    else:
        span = f"{first.strftime('%Y%m%d')}-{last.strftime('%m%d')}"
    return f"{callsign} {grid6(grid)} {span} Meteor Scatter QSO Party"


def all_txt_line(sidecar: dict, decode: dict) -> str:
    """One line as WSJT-X's MainWindow::write_all builds it: the time, "%10.3f " MHz,
    "Rx ", the mode padded to six, then the decoder's own columns, which mskrtd writes as
    i4 (SNR), f5.1 (dt), i5 (audio Hz), and the message.  The time is the recording's start,
    the WAV's name, and dt the decode's offset into it; WSJT-X live stamps an MSK144 line
    with the clock time of the decode instead."""
    t = datetime.fromisoformat(sidecar["slot_start_utc"])
    mhz = sidecar["dial_freq_hz"] / 1e6
    return (f"{t.strftime('%y%m%d_%H%M%S')}{mhz:10.3f} Rx {'MSK144':<6}"
            f"{int(decode['snr']):4d}{float(decode['dt']):5.1f}"
            f"{int(decode['audio_freq_hz']):5d} {decode['message']}\n")


def description_fields(cfg: dict, callsign: str, grid: str, bands: list, version: str):
    comment = ("Receive-only monitor.  Channel AGC off.  Newer recordings are two-channel IEEE float "
               "I/Q (I then Q, 12 kHz) with no gain applied; older ones are 16-bit mono WAVs with "
               "one fixed gain.  `meteor-scatter msqp wav` makes a 16-bit WAV from any I/Q "
               "recording.  The .json sidecar beside each recording holds its timing and every "
               "gain stage, with the RX888 front-end steps given as sample windows; radiod "
               "already rescales the samples at each step, so do not correct twice "
               "(README.txt explains).  "
               f"Software: meteor-scatter {version}.")
    if cfg.get("comments"):
        comment = f"{cfg['comments']}  {comment}"
    values = [callsign, cfg["operator"], cfg["contact_email"], grid6(grid), "Receive Only",
              EQUIPMENT, "NA", cfg["antenna"], ", ".join(bands), comment]
    return list(zip(MSQP_FIELD_ORDER, values))


def description_text(fields) -> str:
    return "".join(f"{k}: {v}\n" for k, v in fields)


def description_html(fields) -> str:
    return "".join(f"<p><strong>{html.escape(k)}:</strong> {html.escape(v)}</p>" for k, v in fields)


def record_metadata(title_text: str, desc_html: str, operator: str, today: date) -> dict:
    return {
        "access": {"record": "public", "files": "public"},
        "files": {"enabled": True},
        "metadata": {
            "resource_type": {"id": "dataset"},
            "title": title_text,
            "publication_date": today.isoformat(),
            "creators": [{"person_or_org": {"type": "personal", "name": operator,
                                            "family_name": operator}}],
            "description": desc_html,
            "rights": [{"id": "cc-by-4.0"}],
        },
    }


README = """\
Meteor-scatter recordings from a receive-only monitor
=====================================================

Software: meteor-scatter {version}.  Receiver: ka9q-radio's radiod on an RX888.

Files
-----
<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav
    One slot (15 s, unless the station set another period) in which jt9
    decoded at least one MSK144 message, named for the slot's start (UTC).
    Slots without a decode are not kept.  The file comes in one of two kinds,
    and the sidecar's "capture" field says which:
      "usb" (or no capture field): an ordinary 12 kHz 16-bit mono WAV.
      "iq": a 12 kHz I/Q recording.  See "The I/Q files" below.
    One package can hold both kinds.  Each recording is packaged as it was kept.
<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.json
    The sidecar: the recording's capture kind, timing, gain, noise floor,
    front-end state and decodes.
ALL.TXT
    Every decode in WSJT-X's ALL.TXT layout, the same for both kinds of
    recording.  Each line's time gives the recording's start (its file name), and dt the decode's offset into it.
    WSJT-X live stamps an MSK144 line with the clock time of the decode instead.
classification.csv
    Every decode with its verdict, by the rules the station's daemon applies
    before sending a spot to PSKReporter: spotted or not, and why; where
    the transmitter's grid came from (the message itself, or a message from the
    same callsign within 24 h); the path length; any suspect flag; and the
    exchange it belongs to, where this monitor heard both stations and an
    acknowledgement.  The daemon also resolves hashed compound callsigns
    from what it has heard before; this file does not, so a decode carrying
    one can read "no transmitter callsign" here though the station spotted it.

The I/Q files
-------------
An I/Q file is a WAV with two channels of 32-bit IEEE float samples.  Channel
1 is I and channel 2 is Q.  The rate is 12 kHz.  The sidecar's iq_center_hz
gives the centre frequency, and iq_filter_hz the passband edges, in Hz above
it.  The file holds radiod's samples as they came: no gain, no normalisation.
The filter keeps only the upper sideband, so the real part of I + jQ is the USB
audio a receiver would give.

To get a WAV like everyone else's, run:

    meteor-scatter msqp wav <stem>.wav -o <stem>.s16.wav

(the sidecar must sit beside the file).  Or do it by hand in numpy:

    import numpy as np
    x = np.fromfile("<stem>.wav", dtype="<f4", offset=46)   # 46-byte header
    iq = x.reshape(-1, 2)                                   # columns: I, Q
    gain = 10 ** (sidecar["scale_db"] / 20)            # scale_db is null if uncalibrated
    pcm = np.clip(np.round(iq[:, 0] * gain * 32768), -32768, 32767).astype("<i2")

That gives the recording the usb capture would have kept, for a calibrated
band.  Where scale_db is null, the command peak-normalises the slot instead.

The sidecar lets an analyst do three things.  The timing block says which
sample is which instant: the exact slot start, the first sample's RTP
timestamp, the anchor that ties RTP time to UTC, and the sample rate.  The
gain_chain block lists every gain stage from antenna to file, each with the key
that records it.  The front-end steps say where the receiver changed gain.  With
those, an analyst can place a ping in UTC and set its level against the noise
floor in absolute terms.

Gain
----
The channel runs with its AGC off at 0 dB gain; the sidecar's channel_requested
and channel_reported show what was asked and what radiod confirmed.  Each usb
WAV carries one fixed gain, wav_gain_db, for the whole file.  An I/Q file
carries no gain of its own, and wav_gain_db there describes only the audio made
for the decoder.  The noise floor
(noise_floor_dbfs) is measured before that gain.

The RX888 front end has an AGC of its own, shared by every channel on the
receiver.  It has no attack or decay time constant.  It acts in steps:

  1. radiod averages the A/D power of the whole digitized band, with a
     100 ms exponential time constant.
  2. About once a second it compares that average with a window,
     -26 to -15 dBFS by default.
  3. Outside the window, it sets the VGA gain in one step, aiming the level at
     the window's middle.  The attenuator never moves.
  4. radiod rescales the samples by the new gain at once, so a signal keeps
     its level across a step.  Do not correct for a step a second time.

A meteor ping adds almost nothing to the power of the whole band, so a ping does
not trigger a step.  Steps follow changes across the whole band, and they are
rare.  Two effects may remain at a step, both unmeasured so far: for about
one USB queue's length (about 0.1 s), samples already in flight carry the
old gain at the new scale; and the receiver's own noise may shift slightly.

frontend.steps in the sidecar lists each step inside a recording: the gain
before and after (from_db, to_db), and sample_window, the samples within which
it happened.  radiod reports the gain in status packets, so a step can be
located only between two packets, about one to two seconds apart.
frontend.basis names the time basis: the host's receive time of those packets.
frontend.missing_sample_windows marks stretches with no status, and
frontend.overloaded marks an A/D overload during the recording.
"""


def readme_text(version: str) -> str:
    return README.format(version=version)
