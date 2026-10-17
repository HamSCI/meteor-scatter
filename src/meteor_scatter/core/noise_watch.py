"""Watch one band's noise floor against its calibrated level.  Warns; never adjusts."""
from __future__ import annotations

from typing import Optional


class NoiseWatch:
    def __init__(self, calibrated_dbfs: Optional[float], threshold_db: float = 10.0,
                 hold_sec: float = 3600.0):
        self._cal = calibrated_dbfs
        self._thr = threshold_db
        self._hold = hold_sec
        self._out_since: Optional[float] = None
        self._gain_at_out: Optional[float] = None
        self._alarmed = False
        self._last: Optional[float] = None

    def observe(self, t: float, noise_dbfs: Optional[float],
                fe_gain_db: Optional[float]) -> Optional[str]:
        if self._cal is None or noise_dbfs is None:
            return None
        self._last = noise_dbfs
        dev = noise_dbfs - self._cal
        if abs(dev) > self._thr:
            if self._out_since is None:
                self._out_since, self._gain_at_out = t, fe_gain_db
            if not self._alarmed and t - self._out_since >= self._hold:
                self._alarmed = True
                fe = ("front-end gain unknown" if fe_gain_db is None or self._gain_at_out is None
                      else f"front-end gain moved {fe_gain_db - self._gain_at_out:+.1f} dB")
                return (f"noise floor {noise_dbfs:.1f} dBFS sits {dev:+.1f} dB from its calibrated "
                        f"{self._cal:.1f} dBFS for an hour ({abs(dev):.1f} dB); {fe} over the same "
                        f"hour; re-run `meteor-scatter msqp calibrate` between showers")
            return None
        self._out_since = None
        if self._alarmed:
            self._alarmed = False
            return f"noise floor back within {self._thr:.0f} dB of calibration ({noise_dbfs:.1f} dBFS)"
        return None

    def state(self) -> dict:
        return {"calibrated_dbfs": self._cal, "last_dbfs": self._last,
                "alarmed": self._alarmed, "since": self._out_since if self._alarmed else None}
