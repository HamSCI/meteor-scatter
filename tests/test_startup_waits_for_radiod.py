"""Startup waits out an absent radiod (AC0G-B2, 2026-10-15 / 2026-11-04)."""
import logging
import threading

import pytest
from ka9q import ConnectionError as RadiodConnectionError

from meteor_scatter.core.recorder import MeteorScatterRecorder


class FakeRx:
    _radiod_id = "bee1"

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def provision_channels(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RadiodConnectionError("Failed to resolve bee1-hf-status.local")


def make(fail_times, stop_after=None):
    rec = object.__new__(MeteorScatterRecorder)
    rec._provision_kwargs = {}
    rec._msqp_runtimes = {}
    rec._stop_event = threading.Event()
    rec.events = []
    rec.delays = []
    rec._sd_notify = lambda m: rec.events.append(m)

    def fake_wait(seconds):
        rec.delays.append(seconds)
        rec.events.append(("wait", seconds))
        return stop_after is not None and len(rec.delays) >= stop_after

    rec._wait = fake_wait
    return rec, FakeRx(fail_times)


def test_startup_waits_for_an_unresolvable_radiod_instead_of_exiting():
    rec, rx = make(3)
    rec._provision_with_backoff(rx)
    assert rx.calls == 4
    assert rec.delays == [5, 10, 20]


def test_delays_cap_at_sixty():
    rec, rx = make(7)
    rec._provision_with_backoff(rx)
    assert rec.delays == [5, 10, 20, 40, 60, 60, 60]


def test_waiting_logs_once_per_episode(caplog):
    rec, rx = make(4)
    with caplog.at_level(logging.INFO):
        rec._provision_with_backoff(rx)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    infos = [r for r in caplog.records if "reachable after" in r.getMessage()]
    assert len(warns) == 1
    assert len(infos) == 1


def test_notify_ready_precedes_the_wait():
    rec, rx = make(2)
    rec._provision_with_backoff(rx)
    first_wait = next(i for i, e in enumerate(rec.events) if isinstance(e, tuple))
    ready = next(i for i, e in enumerate(rec.events)
                 if isinstance(e, bytes) and e.startswith(b"READY=1"))
    assert ready < first_wait
    assert b"STATUS=waiting for radiod bee1" in rec.events[ready]
    assert rec.events[-1] == b"STATUS=running"


def test_stop_signal_ends_the_wait():
    rec, rx = make(99, stop_after=2)
    with pytest.raises(RadiodConnectionError):
        rec._provision_with_backoff(rx)
    assert rec.delays == [5, 10]
