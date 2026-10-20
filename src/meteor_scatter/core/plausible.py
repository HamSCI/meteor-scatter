"""Reject a decode whose grid meteor scatter cannot reach.

jt9 decodes noise now and then.  B4 on its dummy load (10-19) decoded
"MJ3XMF 1B0OCH R CI31": a call-shaped string, and a grid in the open
Pacific.  A callsign test cannot catch it, since short-wave listeners
report spots too and a call's shape proves nothing.  The grid can: a
meteor trail at about 100 km reflects over at most about 2,300 km, so a
grid farther than that from the receiver did not come by meteor.

The limit allows for the size of a 4-character square (about 110 km
from its centre to a corner) at each end of the path.
"""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

MAX_SPOT_KM = 2500.0

_GRID = re.compile(r"^([A-R]{2})([0-9]{2})([A-X]{2})?$")
_EARTH_KM = 6371.0


def grid_center(grid: str) -> Optional[Tuple[float, float]]:
    """(lat, lon) in degrees at the centre of a 4- or 6-character
    Maidenhead locator, or None when ``grid`` is not one."""
    m = _GRID.match(grid.strip().upper())
    if not m:
        return None
    field, square, sub = m.groups()
    lon = (ord(field[0]) - 65) * 20 - 180 + int(square[0]) * 2
    lat = (ord(field[1]) - 65) * 10 - 90 + int(square[1])
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


def out_of_reach(grid: str, host_grid: str,
                 max_km: float = MAX_SPOT_KM) -> Optional[str]:
    """Why a decode carrying ``grid`` cannot have come by meteor scatter
    to a receiver at ``host_grid``, or None when it can, or when either
    grid is missing or malformed (no evidence either way)."""
    there, here = grid_center(grid or ""), grid_center(host_grid or "")
    if there is None or here is None:
        return None
    d = distance_km(here, there)
    if d <= max_km:
        return None
    return (f"grid {grid} lies {d:,.0f} km from {host_grid}; meteor "
            f"scatter reaches about 2,300 km")
