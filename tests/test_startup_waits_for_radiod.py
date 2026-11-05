"""Startup waits out an absent radiod (AC0G-B2, 2026-10-15 / 2026-11-04)."""
import logging
import threading

import pytest
from ka9q import ConnectionError as RadiodConnectionError

import time

from meteor_scatter.core.receiver_manager import RadiodUnavailable
from meteor_scatter.core.recorder import MeteorScatterRecorder, _StopRequested


class FakeRx:
    _radiod_id = "bee1"

    def __init__(self, fail_times, exc=None, log_each=False):
        self.fail_times = fail_times
        self.calls = 0
        self.stops = 0
        self.exc = exc
        self.log_each = log_each

    def stop(self):
        self.stops += 1

    def provision_channels(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.log_each:
                logging.getLogger("ka9q.control").error(
                    "Unexpected error connecting to radiod", exc_info=True)
            raise (self.exc or RadiodConnectionError(
                "Failed to resolve bee1-hf-status.local"))


def make(fail_times, stop_after=None, **kw):
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
    return rec, FakeRx(fail_times, **kw)


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
    with pytest.raises(_StopRequested):
        rec._provision_with_backoff(rx)
    assert rec.delays == [5, 10]


def test_radiod_resolves_but_never_answers_is_retried_too():
    # Name resolves, multicast join succeeds, every verify times out:
    # provision_channels raises RadiodUnavailable.  Same episode, same backoff.
    rec, rx = make(2, exc=RadiodUnavailable("no channels could be provisioned"))
    rec._provision_with_backoff(rx)
    assert rx.calls == 3
    assert rec.delays == [5, 10]
    assert rx.stops == 2  # half-built state torn down before each retry


def test_ka9q_per_try_errors_are_filtered_during_the_episode(caplog):
    rec, rx = make(4, log_each=True)
    with caplog.at_level(logging.DEBUG):
        rec._provision_with_backoff(rx)
    names = [(r.name, r.levelno) for r in caplog.records]
    first_warn = next(i for i, r in enumerate(caplog.records)
                      if r.levelno == logging.WARNING)
    after = [r for r in caplog.records[first_warn + 1:]
             if r.name.startswith("ka9q")]
    assert after == []
    # the filter is gone once the episode ends
    logging.getLogger("ka9q.control").error("later")
    assert caplog.records[-1].getMessage() == "later"


def test_ka9q_filter_removed_on_stop():
    rec, rx = make(99, stop_after=1, log_each=True)
    with pytest.raises(_StopRequested):
        rec._provision_with_backoff(rx)
    assert logging.getLogger("ka9q.control").filters == []


def test_wait_keeps_petting_the_watchdog(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Ev:
        def wait(self, t):
            clock[0] += t
            return False

    monkeypatch.setenv("WATCHDOG_USEC", "120000000")  # slices of 60 s
    rec = object.__new__(MeteorScatterRecorder)
    rec._stop_event = Ev()
    sent = []
    rec._sd_notify = sent.append
    assert rec._wait(150) is False
    assert sent.count(b"WATCHDOG=1") >= 2
    assert clock[0] == 150


def test_stop_during_wait_is_quiet_in_run(caplog):
    rec = object.__new__(MeteorScatterRecorder)
    rec._running = False
    rec._stop_event = threading.Event()
    rec._on_signal = lambda *a: None
    rec._wait_for_chrony_settled = lambda: None

    def boom():
        raise _StopRequested()

    rec._provision_all_receivers = boom
    rec._shutdown = lambda: None
    import signal as _s
    old = (_s.getsignal(_s.SIGTERM), _s.getsignal(_s.SIGINT))
    try:
        with caplog.at_level(logging.INFO):
            rec.run()
    finally:
        _s.signal(_s.SIGTERM, old[0]); _s.signal(_s.SIGINT, old[1])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info]
