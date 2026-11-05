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


class BlockingRx(FakeRx):
    """Fails fast once, then blocks (as a silent radiod does), then succeeds."""

    def __init__(self, block_s):
        super().__init__(0)
        self.block_s = block_s

    def provision_channels(self, **kw):
        self.calls += 1
        if self.calls == 1:
            raise RadiodUnavailable("no channels")
        if self.calls == 2:
            time.sleep(self.block_s)
            raise RadiodUnavailable("no channels")


def test_watchdog_is_petted_while_provisioning_blocks(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")  # period floor 1 s
    rec, _ = make(0)
    rx = BlockingRx(2.5)
    rec._wait = lambda s: False
    rec._provision_with_backoff(rx)
    assert rec.events.count(b"WATCHDOG=1") >= 2
    assert not [t for t in threading.enumerate() if t.name == "watchdog-pet"]


def test_no_watchdog_thread_without_watchdog_usec(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    rec, rx = make(2)
    rec._provision_with_backoff(rx)
    assert b"WATCHDOG=1" not in rec.events
    for bad in ("0", "-5", "junk"):
        monkeypatch.setenv("WATCHDOG_USEC", bad)
        rec, rx = make(1)
        rec._provision_with_backoff(rx)
        assert b"WATCHDOG=1" not in rec.events


def test_watchdog_thread_stopped_after_stop(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    rec, rx = make(99, stop_after=1)
    with pytest.raises(_StopRequested):
        rec._provision_with_backoff(rx)
    assert not [t for t in threading.enumerate() if t.name == "watchdog-pet"]


def test_client_receiver_manager_logs_are_quiet_during_the_episode(caplog):
    from meteor_scatter.core import receiver_manager as rm_mod
    rml = logging.getLogger(rm_mod.__name__)

    class Noisy(FakeRx):
        def provision_channels(self, **kw):
            self.calls += 1
            rml.info("connecting to radiod at x")
            rml.warning("verify timed out, retrying")
            rml.error("could not be verified")
            if self.calls <= self.fail_times:
                raise RadiodUnavailable("no channels")

    rec, _ = make(0)
    rx = Noisy(3)
    with caplog.at_level(logging.DEBUG):
        rec._provision_with_backoff(rx)
        rml.info("after success")
    recs = caplog.records
    w = next(i for i, r in enumerate(recs) if r.name.endswith("core.recorder")
             and r.levelno == logging.WARNING)
    between = [r for r in recs[w + 1:] if r.name == rm_mod.__name__
               and r.getMessage() != "after success"]
    # the last, successful try runs after the filter is removed? no: it runs
    # inside the episode, so it is quiet too
    assert between == []
    assert recs[-1].getMessage() == "after success"


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
