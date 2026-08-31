"""Scoped recovery from a radiod restart, driven by ka9q-python's ladder.

Both recorders already survive a *stall*: _ProgressGate withholds the
systemd watchdog ping after 90 s without pipeline progress and systemd
restarts the unit at two minutes.  That works, and it is blunt -- the
whole process dies, every in-memory sink and decoder with it, for a fault
that may affect one radiod out of several.

The ladder gives the cheaper tier: notice one source has gone quiet and
rebuild just that source.  wspr-recorder worked this out and kept it;
promoting it into ka9q-python (RecoveryLadder) put the policy where every
client can reach it, and this wires psk-recorder to it.

Health here is delivery progress.  A sink whose delivered-sample count
stops advancing is not receiving, whatever its thread is doing -- the
exact state a radiod restart leaves behind, since the SSRC it was reading
no longer exists.
"""
from __future__ import annotations

import unittest

from ka9q import RecoveryAction, RecoveryLadder


class _FakeSink:
    def __init__(self, delivered=0):
        self.delivered = delivered

    def stats_snapshot(self):
        return {"delivered": self.delivered}


class _FakeManager:
    """Stands in for ReceiverManager: sinks plus a scoped reset."""

    def __init__(self, sinks):
        self._sinks = sinks
        self.resets = 0

    @property
    def sinks(self):
        return self._sinks

    def reset_source(self):
        self.resets += 1
        for s in self._sinks:          # a rebuilt source delivers again
            s.delivered += 1


def _delivered(rx):
    return sum(s.stats_snapshot()["delivered"] for s in rx.sinks)


def _tick(rx, ladder, prev):
    """One health observation; returns the new delivered total."""
    now = _delivered(rx)
    action = ladder.observe(healthy=now > prev)
    if action is not RecoveryAction.NONE:
        rx.reset_source()
        return _delivered(rx)
    return now


class ScopedRecovery(unittest.TestCase):
    def test_a_delivering_source_is_left_alone(self):
        rx = _FakeManager([_FakeSink(), _FakeSink()])
        lad = RecoveryLadder()
        prev = 0
        for _ in range(20):
            for s in rx.sinks:
                s.delivered += 10
            prev = _tick(rx, lad, prev)
        self.assertEqual(rx.resets, 0)

    def test_a_silent_source_is_rebuilt(self):
        """No sink advancing is the radiod-restart signature."""
        rx = _FakeManager([_FakeSink(), _FakeSink()])
        lad = RecoveryLadder()
        prev = _delivered(rx)
        prev = _tick(rx, lad, prev)          # first degraded tick acts
        self.assertEqual(rx.resets, 1)

    def test_one_bad_tick_then_recovery_does_not_ratchet(self):
        rx = _FakeManager([_FakeSink()])
        lad = RecoveryLadder()
        prev = _delivered(rx)
        prev = _tick(rx, lad, prev)          # degraded -> reset
        for _ in range(5):                   # healthy again
            rx.sinks[0].delivered += 10
            prev = _tick(rx, lad, prev)
        self.assertEqual(lad.consecutive_degraded, 0)

    def test_a_source_that_stays_dead_keeps_being_retried(self):
        """Going quiet is the failure this exists to prevent."""
        rx = _FakeManager([_FakeSink()])
        rx.reset_source = lambda: None       # rebuild that does not help
        lad = RecoveryLadder()
        prev = _delivered(rx)
        actions = [lad.observe(healthy=False) for _ in range(12)]
        self.assertTrue(all(a is not RecoveryAction.NONE for a in actions[1:]))
        self.assertIs(actions[-1], RecoveryAction.FULL_RESET)

    def test_each_source_escalates_on_its_own(self):
        """One radiod failing must not reset a healthy peer."""
        a, b = _FakeManager([_FakeSink()]), _FakeManager([_FakeSink()])
        la, lb = RecoveryLadder(), RecoveryLadder()
        pa, pb = _delivered(a), _delivered(b)
        for _ in range(4):
            b.sinks[0].delivered += 5        # b healthy, a silent
            pa = _tick(a, la, pa)
            pb = _tick(b, lb, pb)
        self.assertGreater(a.resets, 0)
        self.assertEqual(b.resets, 0)


if __name__ == "__main__":
    unittest.main()
