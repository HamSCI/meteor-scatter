"""Decode-backlog monitor — notice when the MSK144 decoders stop keeping up.

wsprdaemon added the equivalent (wd-decode-backlog.sh) after K6FOD's decoder,
starved by a 1.4 GHz clock cap, silently built a 42 GB / 3600-file FT8 pile-up
that nothing watched.  meteor-scatter forks one `jt9 --msk144` per completed
slot, so a pile-up shows up as decodes still in flight when the next slot
fires.  Per (radiod, mode):

  overdue   the oldest in-flight decode is older than ``warn_age_s`` (default
            45 s — three 15 s MSK144 slots, and 15 s short of the 60 s kill
            deadline that would otherwise be the first anyone heard of it)
  saturated in-flight decodes exceed ``ratio`` x channels (default 1.0: on
            average every channel has a decode outstanding) for
            ``sustain_samples`` consecutive 60 s samples (default 3)

Transitions are logged: WARNING on entry, a reminder every ``remind_s`` while
it persists, INFO on recovery.  Thresholds: METEOR_SCATTER_BACKLOG_WARN_AGE_SEC,
METEOR_SCATTER_BACKLOG_WARN_INFLIGHT_RATIO, METEOR_SCATTER_BACKLOG_SUSTAIN_SAMPLES.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_WARN_AGE_S = 45.0
DEFAULT_RATIO = 1.0
DEFAULT_SUSTAIN = 3
DEFAULT_REMIND_S = 600.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Assessment:
    level: str        # "ok" | "warn"
    reason: str
    inflight: int
    oldest_s: float


class DecodeBacklogMonitor:
    """One instance per (radiod_id, mode); feed it one aggregate per minute."""

    def __init__(self, key: str, *, warn_age_s: Optional[float] = None,
                 ratio: Optional[float] = None, sustain_samples: Optional[int] = None,
                 remind_s: float = DEFAULT_REMIND_S, log: logging.Logger = logger) -> None:
        self.key = key
        self.warn_age_s = warn_age_s if warn_age_s is not None else \
            _env_float("METEOR_SCATTER_BACKLOG_WARN_AGE_SEC", DEFAULT_WARN_AGE_S)
        self.ratio = ratio if ratio is not None else _env_float("METEOR_SCATTER_BACKLOG_WARN_INFLIGHT_RATIO", DEFAULT_RATIO)
        self.sustain = sustain_samples if sustain_samples is not None else \
            int(_env_float("METEOR_SCATTER_BACKLOG_SUSTAIN_SAMPLES", DEFAULT_SUSTAIN))
        self.remind_s = remind_s
        self._log = log
        self._saturated_run = 0
        self._last: Optional[Assessment] = None
        self._warn_since: Optional[float] = None
        self._last_logged = 0.0

    def assess(self, inflight: int, oldest_s: float, channels: int) -> Assessment:
        if inflight and oldest_s > self.warn_age_s:
            self._saturated_run += 1
            return Assessment("warn", f"{self.key}: decode backlog OVERDUE — oldest of {inflight} in-flight "
                                      f"decode(s) is {oldest_s:.0f}s old (> {self.warn_age_s:.0f}s); the "
                                      f"decoders are not keeping up with the slot cadence", inflight, oldest_s)
        if channels and inflight > self.ratio * channels:
            self._saturated_run += 1
            if self._saturated_run >= self.sustain:
                return Assessment("warn", f"{self.key}: decode backlog SATURATED — {inflight} decode(s) in "
                                          f"flight for {channels} channel(s) over {self._saturated_run} "
                                          f"consecutive samples", inflight, oldest_s)
        else:
            self._saturated_run = 0
        return Assessment("ok", f"{self.key}: {inflight} in flight, oldest {oldest_s:.0f}s", inflight, oldest_s)

    def observe(self, inflight: int, oldest_s: float, channels: int, now: float) -> Assessment:
        a = self.assess(inflight, oldest_s, channels)
        was_warn = self._last is not None and self._last.level == "warn"
        if a.level == "warn":
            if not was_warn:
                self._warn_since = now
                self._last_logged = now
                self._log.warning("%s", a.reason)
            elif now - self._last_logged >= self.remind_s:
                self._last_logged = now
                self._log.warning("%s — persisting for %.0f min", a.reason, (now - (self._warn_since or now)) / 60)
        elif was_warn:
            self._log.info("%s: decode backlog cleared after %.0f min", self.key, (now - (self._warn_since or now)) / 60)
            self._warn_since = None
        self._last = a
        return a


class BacklogMonitors:
    """Registry keyed by 'rx:mode'; creates monitors on first sight."""

    def __init__(self) -> None:
        self._by_key: Dict[str, DecodeBacklogMonitor] = {}

    def observe(self, key: str, inflight: int, oldest_s: float, channels: int, now: float) -> Assessment:
        mon = self._by_key.get(key)
        if mon is None:
            mon = self._by_key[key] = DecodeBacklogMonitor(key)
        return mon.observe(inflight, oldest_s, channels, now)
