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
    t = datetime.fromisoformat(sidecar["slot_start_utc"])
    mhz = sidecar["dial_freq_hz"] / 1e6
    return (f"{t.strftime('%y%m%d_%H%M%S')} {mhz:9.3f} Rx {'MSK144':<6} {int(decode['snr']):6d} "
            f"{float(decode['dt']):4.1f} {int(decode['audio_freq_hz']):4d} {decode['message']}\n")


def description_fields(cfg: dict, callsign: str, grid: str, bands: list, version: str):
    comment = ("Receive-only monitor.  Channel AGC off; each WAV carries one fixed gain, recorded "
               "with the RX888 front-end gain in the .json sidecar beside it (front-end steps given "
               f"as sample windows).  Software: meteor-scatter {version}.")
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
