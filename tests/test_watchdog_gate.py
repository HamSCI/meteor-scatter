"""Watchdog liveness gate: pet systemd only while the data path advances.

_ProgressGate decides whether to send WATCHDOG=1.  A wedged (not crashed)
daemon stops advancing its progress signal -> the gate withholds the ping ->
systemd's WatchdogSec restarts it.  A healthy-but-idle recorder keeps
advancing (psk: slot counters; wspr: RTP samples) and keeps pinging.
"""
from __future__ import annotations

import unittest

from msk144_recorder.core.recorder import _ProgressGate


class ProgressGateTests(unittest.TestCase):
    def test_advancing_always_pings(self):
        g = _ProgressGate(stall_sec=90.0)
        t, prog = 0.0, 0
        for _ in range(40):
            prog += 1
            self.assertTrue(g.update(prog, t))
            t += 5

    def test_frozen_after_seen_withholds_past_stall(self):
        g = _ProgressGate(stall_sec=90.0)
        self.assertTrue(g.update(5, 0.0))     # first obs
        self.assertTrue(g.update(6, 5.0))     # advanced -> seen
        self.assertTrue(g.update(6, 90.0))    # 85s since advance: still ping
        self.assertFalse(g.update(6, 100.0))  # 95s since advance: withhold

    def test_dead_from_start_withholds_after_startup_grace(self):
        g = _ProgressGate(stall_sec=90.0)     # startup_grace = 180
        self.assertTrue(g.update(7, 0.0))
        self.assertTrue(g.update(7, 180.0))   # within grace
        self.assertFalse(g.update(7, 181.0))  # past grace -> withhold

    def test_none_progress_fails_safe(self):
        g = _ProgressGate(stall_sec=90.0)
        for t in range(0, 300, 5):
            self.assertTrue(g.update(None, float(t)))

    def test_recovery_re_enables_ping(self):
        g = _ProgressGate(stall_sec=90.0)
        g.update(1, 0.0)
        g.update(2, 5.0)
        self.assertFalse(g.update(2, 100.0))  # stalled
        self.assertTrue(g.update(3, 105.0))   # progress resumed


if __name__ == "__main__":
    unittest.main()
