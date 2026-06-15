"""
AuthorityReader — reads /run/hf-timestd/authority.json published by
hf-timestd's authority manager. Consumer side of the schema v1 contract
documented in hf-timestd/docs/METROLOGY.md §4.5.2.

Under the RTP-reference labeling invariant, meteor-scatter anchors the
UTC of each sample from RTP counters plus a published offset. This
module produces that offset; it is wire-compatible with the
wspr-recorder sibling (same JSON schema).

Standalone fallback. sigmond clients must work without hf-timestd. In
that case `read()` returns None and callers fall back to the system
clock (ONCE, at stream start) with a clear warning. The operator is
responsible for ensuring radiod's host has timing accurate enough for
the correlation to land on the right cadence boundary (~1 s for FT8,
tighter for FT4).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_SUPPORTED_SCHEMAS = {"v1"}

DEFAULT_PATH = Path("/run/hf-timestd/authority.json")
DEFAULT_FRESHNESS_SEC = 60.0


@dataclass
class AuthoritySnapshot:
    """One reading of authority.json. All fields map 1:1 to the published
    schema; see hf-timestd/docs/METROLOGY.md §4.5.2."""
    utc_published: datetime
    a_level: str
    t_level_active: Optional[str]
    t_level_available: List[str]
    t_level_witnesses: List[str]
    rtp_to_utc_offset_ns: Optional[int]
    sigma_ns: Optional[int]
    stations_contributing: List[str]
    last_transition_utc: Optional[str]
    disagreement_flags: List[str]
    governor_radiod: Optional[str] = None

    @property
    def offset_usable(self) -> bool:
        """True iff the snapshot carries a concrete offset we can apply."""
        return (
            self.t_level_active is not None
            and self.rtp_to_utc_offset_ns is not None
        )

    @property
    def offset_seconds(self) -> float:
        """rtp_to_utc_offset_ns expressed as a float in seconds. Undefined
        when `offset_usable` is False."""
        return (self.rtp_to_utc_offset_ns or 0) / 1_000_000_000.0

    def to_timing_authority(
        self, client_radiod: Optional[str] = None,
    ) -> dict:
        """Canonical timing-provenance block for data records.

        Identical across all sigmond clients (wspr/psk/msk144/codar): the
        single authoritative record of how a sample's UTC label was
        derived, sourced entirely from hf-timestd's adjudicated
        authority.json — never from a secondary status feed. Carries tier
        provenance, the applied offset, the honest sigma (including the
        cross-check uncertainty widening), the disagreement flags, and
        multi-radiod attribution. See CLIENT-CONTRACT §18 / METROLOGY
        §4.5. Use standalone_timing_authority() when no snapshot is
        available (hf-timestd absent / stale)."""
        return {
            "source": "hf-timestd-authority",
            "schema": "v1",
            "a_level": self.a_level,
            "t_level_active": self.t_level_active,
            "t_level_witnesses": list(self.t_level_witnesses),
            "rtp_to_utc_offset_ns": self.rtp_to_utc_offset_ns,
            "sigma_ns": self.sigma_ns,
            "disagreement_flags": list(self.disagreement_flags),
            "governor_radiod": self.governor_radiod,
            "client_radiod": client_radiod,
            "authority_utc_published": self.utc_published.isoformat(),
        }


class AuthorityReader:
    """Atomic reader for /run/hf-timestd/authority.json.

    All error paths return None rather than raising, so callers can
    treat "file missing" identically to "hf-timestd not running."
    """

    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        freshness_sec: float = DEFAULT_FRESHNESS_SEC,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.path = Path(path)
        self.freshness_sec = float(freshness_sec)
        self.now_fn = now_fn

    def read(self) -> Optional[AuthoritySnapshot]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("authority.json read error: %s", e)
            return None

        if data.get("schema") not in _SUPPORTED_SCHEMAS:
            logger.debug("authority.json unsupported schema: %r", data.get("schema"))
            return None

        try:
            pub = _parse_iso_z(str(data["utc_published"]))
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("authority.json utc_published parse: %s", e)
            return None

        if (self.now_fn() - pub).total_seconds() > self.freshness_sec:
            return None

        try:
            return AuthoritySnapshot(
                utc_published=pub,
                a_level=str(data.get("a_level", "A1")),
                t_level_active=data.get("t_level_active"),
                t_level_available=list(data.get("t_level_available") or []),
                t_level_witnesses=list(data.get("t_level_witnesses") or []),
                rtp_to_utc_offset_ns=(
                    int(data["rtp_to_utc_offset_ns"])
                    if data.get("rtp_to_utc_offset_ns") is not None
                    else None
                ),
                sigma_ns=(
                    int(data["sigma_ns"])
                    if data.get("sigma_ns") is not None
                    else None
                ),
                stations_contributing=list(data.get("stations_contributing") or []),
                last_transition_utc=data.get("last_transition_utc"),
                disagreement_flags=list(data.get("disagreement_flags") or []),
                governor_radiod=(
                    str(data["governor_radiod"])
                    if data.get("governor_radiod")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("authority.json field error: %s", e)
            return None


def standalone_timing_authority(
    client_radiod: Optional[str] = None,
) -> dict:
    """Canonical timing-provenance block when authority.json is
    unavailable (hf-timestd absent or stale) — the standalone fallback.

    Records explicitly that no hf-timestd offset was applied, so
    downstream analysis sees the provenance directly instead of inferring
    it from a missing block. Shape matches
    AuthoritySnapshot.to_timing_authority so the record key is uniform
    across both states and across all clients."""
    return {
        "source": "standalone-fallback",
        "schema": "v1",
        "a_level": None,
        "t_level_active": None,
        "t_level_witnesses": [],
        "rtp_to_utc_offset_ns": None,
        "sigma_ns": None,
        "disagreement_flags": [],
        "governor_radiod": None,
        "client_radiod": client_radiod,
        "authority_utc_published": None,
    }


def _parse_iso_z(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
