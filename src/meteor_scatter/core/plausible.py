"""Classify each MSK144 decode: spot it, or not, and flag what looks wrong.

jt9 decodes noise now and then.  B4 on its dummy load (10-19) decoded
"MJ3XMF 1B0OCH R CI31": a call-shaped string, and a grid in the open
Pacific.  Meteor-scatter data serve three purposes, and each needs its
own confidence, so a decode gets classified once and each outlet takes
what it needs (Michael, 10-20):

- SWL reports (PSKReporter) take a decode only when nothing in it is
  plainly wrong: the transmitter has a valid callsign and a grid, and
  the receiver has a grid.  The receiver needs no callsign, since
  short-wave listeners report too.
- Science (the MSQP Zenodo package) takes every archived decode with
  its classification, so an analyst can choose the confidence.
- Contest (MSQP) rules belong to the organizers.  heard_exchanges()
  marks the decodes that belong to an exchange this station heard both
  sides of, the strongest evidence a monitor can offer.

A decode with no grid of its own takes the grid this station decoded
from the same callsign within 24 hours: in a meteor-scatter QSO the
grid goes out once, and the reports, RRR and 73 that follow carry none.

Anything not plainly wrong but unlikely passes with a suspect flag.  A
grid beyond meteor-scatter range is one: WSJT-X gives "up to about 2100
km", and the limit adds the size of a square at each end.  Sporadic-E
can carry a real MSK144 signal farther on 6 m, so it gets flagged, not
dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from callhash.parse import _CALL_RE

MAX_SPOT_KM = 2500.0
GRID_MEMORY_S = 24 * 3600.0
EXCHANGE_WINDOW_S = 3600.0

# 4, 6, 8 or 10 characters; past six, the extra precision changes no
# distance this module judges, so it reads the first six.
_GRID = re.compile(r"^([A-R]{2})([0-9]{2})(?:([A-X]{2})(?:[0-9]{2}(?:[A-X]{2})?)?)?$")
_EARTH_KM = 6371.0
_ACK = re.compile(r"^(R|RRR|RR73|73|R[+-]\d+)$")

REJECT_NO_CALL = "no transmitter callsign"
REJECT_BAD_CALL = "transmitter callsign not ITU-allocated"
REJECT_NO_GRID = "no transmitter grid"
REJECT_HOST_GRID = "receiver grid missing or malformed"
SUSPECT_REACH = "beyond meteor-scatter reach"


def grid_center(grid: str) -> Optional[Tuple[float, float]]:
    """(lat, lon) in degrees at the centre of a Maidenhead locator (to
    six characters), or None when ``grid`` is not one."""
    m = _GRID.match((grid or "").strip().upper())
    if not m or m.group(0) == "RR73":
        return None
    field_, square, sub = m.groups()
    lon = (ord(field_[0]) - 65) * 20 - 180 + int(square[0]) * 2
    lat = (ord(field_[1]) - 65) * 10 - 90 + int(square[1])
    if sub:
        lon += (ord(sub[0]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(sub[1]) - 65) * (1 / 24) + 1 / 48
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def tx_call_valid(call: str) -> bool:
    """A callsign's shape, from a prefix series the ITU allocates.  The
    ITU allocates no series beginning with 0, 1 or Q; 1A (the Sovereign
    Military Order of Malta, a DXCC entity) is the one such prefix in
    use.  For "VE3/K1ABC" the prefix before the slash decides."""
    call = (call or "").strip().upper()
    if not call or not _CALL_RE.match(call):
        return False
    if call[0] == "1":
        return call.startswith("1A")
    return call[0] not in "0Q"


@dataclass
class Verdict:
    spot: bool
    grid: str = ""
    grid_source: str = "none"       # "message", "recent" or "none"
    distance_km: Optional[float] = None
    rejected: List[str] = field(default_factory=list)
    suspect: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"spot": self.spot, "grid": self.grid,
                "grid_source": self.grid_source,
                "distance_km": (None if self.distance_km is None
                                else round(self.distance_km)),
                "rejected": list(self.rejected),
                "suspect": list(self.suspect)}


class Classifier:
    """Classifies decodes in time order.  It remembers the grid each
    valid callsign sent, for ``memory_s``, to place that callsign's later
    messages."""

    def __init__(self, host_grid: str, max_km: float = MAX_SPOT_KM,
                 memory_s: float = GRID_MEMORY_S) -> None:
        self.host_grid = (host_grid or "").strip()
        self._here = grid_center(self.host_grid)
        self._max_km = max_km
        self._memory_s = memory_s
        self._grids: Dict[str, Tuple[float, str]] = {}

    @property
    def host_grid_ok(self) -> bool:
        return self._here is not None

    def classify(self, when: float, tx_call: str, grid: str) -> Verdict:
        v = Verdict(spot=False)
        call = (tx_call or "").strip().upper()
        if not call:
            v.rejected.append(REJECT_NO_CALL)
        elif not tx_call_valid(call):
            v.rejected.append(REJECT_BAD_CALL)
        if grid_center(grid) is not None:
            v.grid, v.grid_source = grid.strip(), "message"
            if not v.rejected:
                self._grids[call] = (when, v.grid)
        elif call and not v.rejected:
            seen = self._grids.get(call)
            if seen and 0 <= when - seen[0] <= self._memory_s:
                v.grid, v.grid_source = seen[1], "recent"
        if not v.grid:
            v.rejected.append(REJECT_NO_GRID)
        if self._here is None:
            v.rejected.append(REJECT_HOST_GRID)
        elif v.grid:
            v.distance_km = distance_km(self._here, grid_center(v.grid))
            if v.distance_km > self._max_km:
                v.suspect.append(SUSPECT_REACH)
        v.spot = not v.rejected
        return v


def heard_exchanges(decodes: Sequence[dict],
                    window_s: float = EXCHANGE_WINDOW_S) -> Dict[int, str]:
    """{index: "A<>B"} for each decode that belongs to an exchange this
    station heard: A sent to B and B sent to A within ``window_s`` of each
    other, and one of those messages acknowledges ("R", "R+05", "RRR",
    "RR73", "73").  ``decodes`` carry ``time``, ``tx_call``, ``rx_call``
    and ``message``."""
    pairs: Dict[frozenset, List[int]] = {}
    for i, d in enumerate(decodes):
        tx, rx = (d.get("tx_call") or "").upper(), (d.get("rx_call") or "").upper()
        if tx and rx and tx != rx:
            pairs.setdefault(frozenset((tx, rx)), []).append(i)
    out: Dict[int, str] = {}
    for pair, idx in pairs.items():
        a, b = sorted(pair)
        members = set()
        for i in idx:
            for j in idx:
                di, dj = decodes[i], decodes[j]
                if (di["tx_call"].upper() == a and dj["tx_call"].upper() == b
                        and abs(di["time"] - dj["time"]) <= window_s
                        and (_acks(di["message"]) or _acks(dj["message"]))):
                    members.update((i, j))
        for i in members:
            out[i] = f"{a}<>{b}"
    return out


def _acks(message: str) -> bool:
    return any(_ACK.match(t) for t in (message or "").upper().split()[2:])
