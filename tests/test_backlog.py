"""Tests for the decode-backlog monitor (overdue / saturated) and the
in-flight snapshot it reads.  Same design as psk-recorder's; ported from
wsprdaemon's wd-decode-backlog.sh after K6FOD's unnoticed FT8 pile-up."""

import logging

from meteor_scatter.core.backlog import BacklogMonitors, DecodeBacklogMonitor
from meteor_scatter.core.slot import inflight_from


def test_inflight_from_counts_and_ages_forked_decoders():
    procs = [(None, None, 0.0, 100.0), (None, None, 0.0, 130.0)]   # forked at mono 100, 130
    assert inflight_from(procs, now_mono=150.0) == (2, 50.0)
    assert inflight_from([], now_mono=0.0) == (0, 0.0)


def test_overdue_warns_at_once():
    m = DecodeBacklogMonitor("rx:MSK144", warn_age_s=45, ratio=1.0, sustain_samples=3)
    a = m.assess(inflight=1, oldest_s=50, channels=2)
    assert a.level == "warn" and "OVERDUE" in a.reason


def test_saturated_needs_sustained_samples():
    m = DecodeBacklogMonitor("rx:MSK144", warn_age_s=45, ratio=1.0, sustain_samples=3)
    levels = [m.assess(inflight=4, oldest_s=20, channels=2).level for _ in range(3)]
    assert levels == ["ok", "ok", "warn"]


def test_one_decode_per_channel_is_normal():
    m = DecodeBacklogMonitor("rx:MSK144", warn_age_s=45, ratio=1.0, sustain_samples=3)
    for _ in range(5):
        assert m.assess(inflight=2, oldest_s=8, channels=2).level == "ok"


def test_registry_logs_entry_and_recovery(caplog):
    reg = BacklogMonitors()
    with caplog.at_level(logging.INFO, logger="meteor_scatter.core.backlog"):
        reg.observe("rx:MSK144", 3, 50, 2, now=0)
        reg.observe("rx:MSK144", 3, 55, 2, now=60)
        reg.observe("rx:MSK144", 0, 0, 2, now=120)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("OVERDUE" in x for x in msgs) == 1
    assert any("cleared" in x for x in msgs)
