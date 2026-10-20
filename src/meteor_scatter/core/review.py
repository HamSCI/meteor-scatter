"""Classify the archived decodes of a date range, for review and for the
MSQP package.  The daemon classifies each decode as it spots it
(ch_tailer); this module repeats that over the archive in time order,
with the same Classifier, and adds heard_exchanges(), which needs both
sides of an exchange and so only a review can see."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from callhash import parse_message

from meteor_scatter.core.plausible import (REJECT_REPEAT, Classifier, RepeatFilter,
                                           channel_of, heard_exchanges)

CSV_FIELDS = ["utc", "band", "recording", "frequency_hz", "snr_db", "dt", "message",
              "tx_call", "rx_call", "grid", "grid_source", "distance_km", "spot",
              "rejected", "suspect", "heard_exchange"]


def sidecar_decodes(side: dict, recording: str) -> List[dict]:
    """The decodes one sidecar holds, parsed and timed (slot start + dt)."""
    try:
        start = datetime.fromisoformat(side["slot_start_utc"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return []
    dial = int(side.get("dial_freq_hz") or 0)
    out = []
    for d in side.get("decodes") or []:
        message = str(d.get("message", "")).strip()
        if not message:
            continue
        parsed = parse_message(message)
        out.append({
            "time": start + float(d.get("dt") or 0.0),
            "slot": int(start),
            "band": side.get("band", ""),
            "recording": recording,
            "frequency_hz": dial + int(d.get("audio_freq_hz") or 0),
            "snr_db": d.get("snr"),
            "dt": d.get("dt"),
            "message": parsed["message"],
            "tx_call": parsed.get("tx_call", ""),
            "rx_call": parsed.get("rx_call", ""),
            "message_grid": parsed.get("grid", ""),
        })
    return out


def classify(decodes: Iterable[dict], host_grid: str) -> List[dict]:
    """Each decode, in time order, with its classification and exchange, by
    the daemon's rules: the same Classifier, and the same RepeatFilter."""
    rows = sorted(decodes, key=lambda d: d["time"])
    c = Classifier(host_grid)
    repeats = RepeatFilter()
    exchanges = heard_exchanges(rows)
    out = []
    for i, d in enumerate(rows):
        if repeats.repeat(d["slot"], channel_of(d["frequency_hz"]), d["message"]):
            v = {"spot": False, "grid": "", "grid_source": "none", "distance_km": None,
                 "rejected": [REJECT_REPEAT], "suspect": []}
        else:
            v = c.classify(d["time"], d["tx_call"], d["message_grid"]).as_dict()
        out.append({**d, **v, "heard_exchange": exchanges.get(i, "")})
    return out


def to_csv(rows: Iterable[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({**r,
                    "utc": datetime.fromtimestamp(r["time"], timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "rejected": "; ".join(r["rejected"]),
                    "suspect": "; ".join(r["suspect"])})
    return buf.getvalue()


def summary(rows: List[dict]) -> Tuple[int, int, int, int]:
    """(decodes, spotted, spotted with a suspect flag, in a heard exchange)."""
    return (len(rows), sum(r["spot"] for r in rows),
            sum(r["spot"] and bool(r["suspect"]) for r in rows),
            sum(bool(r["heard_exchange"]) for r in rows))
