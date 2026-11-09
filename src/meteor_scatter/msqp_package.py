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


IQ_DESCRIPTION = ("  Some recordings are two-channel IEEE float I/Q (I then Q, 12 kHz) with no "
                  "gain applied, and the rest are 16-bit mono WAVs; each .json sidecar's "
                  "\"capture\" field says which.  README.txt shows how to make a 16-bit WAV "
                  "from the I/Q (numpy, or `meteor-scatter msqp wav`).")


def description_fields(cfg: dict, callsign: str, grid: str, bands: list, version: str,
                       has_iq: bool = False):
    comment = ("Receive-only monitor.  Channel AGC off; each WAV carries one fixed gain, recorded "
               "with the RX888 front-end gain in the .json sidecar beside it (front-end steps given "
               "as sample windows; radiod already rescales the samples at each step, so do not "
               "correct twice; README.txt explains).  "
               f"Software: meteor-scatter {version}.")
    if has_iq:
        comment += IQ_DESCRIPTION
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
    Slots without a decode are not kept.
<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.json
    The sidecar: the recording's gain, noise floor, front-end state and decodes.
ALL.TXT
    Every decode in WSJT-X's ALL.TXT layout.  Each line's time gives the
    recording's start (its WAV name), and dt the decode's offset into it.
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

Gain
----
The channel runs with its AGC off at 0 dB gain; the sidecar's channel_requested
and channel_reported show what was asked and what radiod confirmed.  Each WAV
carries one fixed gain, wav_gain_db, for the whole file.  The noise floor
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


IQ_README = """
Two kinds of recording, and the I/Q files
-----------------------------------------
This package holds I/Q recordings.  The sidecar's "capture" field says which
kind each file is: "iq" for I/Q, "usb" (or no field at all) for the 16-bit mono
WAV described above.  Both kinds can sit side by side.  ALL.TXT reads the same
for both.

I/Q is the radio's signal kept as two numbers per sample: the in-phase part
(I) and the quadrature part (Q).  It keeps the phase and frequency of a ping,
and not only its loudness.  Its real part, I, is the USB audio you know from
WSJT-X.

The format: a WAV file with two channels of little-endian IEEE 32-bit float,
I first, then Q, at 12 kHz.  The sidecar's iq_center_hz gives the centre
frequency and iq_filter_hz the passband edges, in Hz above it.  The samples
are radiod's own: no gain, no normalisation.  The filter keeps only the upper
sideband, so the real part is the USB audio a receiver would give.

To get a 16-bit WAV like the usb recordings, run this numpy code.  Put the
file's name where <stem> stands.  It reads the file by its chunks, takes the
real part, applies the sidecar's gain and writes samples that match
`meteor-scatter msqp wav` exactly.  A null scale_db means the band was not
calibrated, and the code then peak-normalises the slot to -1 dBFS, as the usb
recordings were.

    import json, struct
    import numpy as np
    side = json.load(open("<stem>.json"))
    raw = open("<stem>.wav", "rb").read()
    pos = 12
    while raw[pos:pos + 4] != b"data":
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        pos += 8 + size + (size & 1)
    n = struct.unpack_from("<I", raw, pos + 4)[0] // 8
    a = np.frombuffer(raw, "<f4", count=2 * n, offset=pos + 8).reshape(-1, 2)[:, 0]
    a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
    db = side["scale_db"]
    if db is None:
        peak = float(np.abs(a).max()) if a.size else 0.0
        target = 32767.0 * 10.0 ** (-1.0 / 20.0)
        db = 20.0 * np.log10((target / peak) / 32767.0) if peak > 0 else 0.0
    pcm = np.clip(np.rint(a.astype(np.float64) * (32767.0 * 10.0 ** (db / 20.0))),
                  -32768, 32767).astype("<i2")

pcm holds the 12 kHz 16-bit mono samples.  Write them with the wave module or
scipy.io.wavfile.

If you prefer a command, install meteor-scatter from
https://github.com/HamSCI/meteor-scatter and run

    meteor-scatter msqp wav <stem>.wav -o <out>.wav

With no -o it writes <stem>.s16.wav in the current directory.  It refuses an
output path inside the station's archive.

What the sidecar's fields let an analyst do.

Timing.  The "timing" block labels each sample in UTC.  Its fields are
slot_start_utc_exact (the requested slot boundary), first_sample_rtp (the RTP
timestamp of sample 0), anchor_rtp, label_anchor_utc, first_sample_offset,
batch_rtp_basis, sample_rate and the anchor details.  The block's own notes
give the rule:

    utc(sample i) = label_anchor_utc + (first_sample_offset + i) / sample_rate

and first_sample_rtp = (anchor_rtp + first_sample_offset) mod 2**32.

How far to trust those labels depends on batch_rtp_basis.  With
"delivered_rtp_start" the receiving software reported the RTP timestamp of
each batch's first sample, and the labels hold to one sample.  With
"last_packet_minus_n" an older version gave only the last packet's header;
each sample then lies one radio packet later than the rule says (about 20 ms
if the radio sends one 20 ms frame per packet), and more where packets were
lost.  "mixed" means the basis changed during the recording, and a null
basis means none was recorded.

Gain.  gain_chain lists every gain stage from the antenna to the file, in
order, each with the key that records it.  Those keys are frontend (the RF
gain, attenuation and AGC steps), channel_requested and channel_reported
(radiod's channel gain), scale_db (the band's calibration) and wav_gain_db
(the gain on the audio made for the decoder).  The stored I/Q carries no gain
of its own.  noise_floor_dbfs is measured before wav_gain_db, so an analyst
can set a ping's level against the noise in absolute terms.

`msqp package` checks each file's header against its sidecar.  It skips any
file whose header contradicts the sidecar, an iq sidecar over a file that is
not two-channel float, or a usb sidecar over one that is, and says why.
"""


def readme_text(version: str, has_iq: bool = False) -> str:
    text = README.format(version=version)
    return text + IQ_README if has_iq else text
