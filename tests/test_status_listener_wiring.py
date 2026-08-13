"""audit F19: wire ka9q.StatusListener so slide-follow tracks radiod's
live RTP<->UTC drift.

ChannelSink._anchor_utc_now() re-reads channel_info.gps_time/rtp_timesnap
every tick, but nothing refreshed that object in place — psk-recorder
starts a StatusListener and registers each channel's ChannelInfo so
radiod's ~2 Hz STATUS broadcasts mutate it live.  These tests assert the
same wiring, mirrored from psk-recorder's receiver_manager.py.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from meteor_scatter.core.receiver_manager import ReceiverManager
# provision_channels() lazily imports meteor_scatter.core.stream (it in
# turn imports numpy + real `ka9q.SlotClock`/`SlotClockDesyncError`).
# Force that import for real, against the REAL ka9q, *before* any test
# below substitutes a fake `ka9q` into sys.modules — otherwise the first
# test to call provision_channels() under the patched sys.modules would
# permanently cache meteor_scatter.core.stream bound to the fake
# SlotClock, corrupting every other test module (e.g.
# test_stream_anchoring.py) that imports the real thing afterwards.
import meteor_scatter.core.stream  # noqa: F401


def _fake_ka9q_modules():
    """Minimal fake ka9q + ka9q.status_listener for provisioning paths.

    provision_channels() lazily imports meteor_scatter.core.stream, whose
    module-level `from ka9q import SlotClock, SlotClockDesyncError` must
    resolve against whatever `sys.modules["ka9q"]` is at that moment — so
    the fake needs those two names too, even though this file's tests
    never touch SlotClock behavior directly (that's F18's job).
    """
    ka9q = types.ModuleType("ka9q")
    ka9q.RadiodControl = MagicMock(name="RadiodControl")
    ka9q.MultiStream = MagicMock(name="MultiStream")
    ka9q.SlotClock = MagicMock(name="SlotClock")

    class _FakeSlotClockDesyncError(Exception):
        pass

    ka9q.SlotClockDesyncError = _FakeSlotClockDesyncError
    sl_mod = types.ModuleType("ka9q.status_listener")
    sl_mod.StatusListener = MagicMock(name="StatusListener")
    ka9q.status_listener = sl_mod
    return {"ka9q": ka9q, "ka9q.status_listener": sl_mod}


def _make_rx(radiod_block=None):
    return ReceiverManager(
        config={"paths": {}, "station": {}, "processing": {}},
        radiod_block=radiod_block or {"status": "rx.local"},
        spool_root=Path("/tmp/ms-test-spool"),
        log_dir=Path("/tmp/ms-test-log"),
        radiod_lifetime_frames=0,
    )


class StatusListenerStartTests(unittest.TestCase):

    def test_provision_starts_status_listener(self):
        """provision_channels must start a StatusListener on the radiod's
        status address right after creating the RadiodControl (psk-recorder
        pattern).  A zero-band radiod_block makes provisioning raise
        RuntimeError at the end ("no channels could be provisioned"); the
        listener must already be up and recorded by then."""
        mods = _fake_ka9q_modules()
        rx = _make_rx()
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(RuntimeError):
                rx.provision_channels(
                    decoder="", decoder_kind="jt9",
                    keep_wav=False, spool_spots=False,
                )
        sl_cls = mods["ka9q.status_listener"].StatusListener
        sl_cls.assert_called_once_with("rx.local")
        sl_cls.return_value.start.assert_called_once_with()
        self.assertIs(rx._status_listener, sl_cls.return_value)

    def test_listener_failure_does_not_block_provisioning(self):
        """Best-effort: StatusListener blowing up must not abort
        provisioning (psk-recorder wraps the whole block in try/except)."""
        mods = _fake_ka9q_modules()
        mods["ka9q.status_listener"].StatusListener.side_effect = OSError("no socket")
        rx = _make_rx()
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(RuntimeError):   # zero bands, as above
                rx.provision_channels(
                    decoder="", decoder_kind="jt9",
                    keep_wav=False, spool_spots=False,
                )
        self.assertIsNone(rx._status_listener)


class RegisterChannelTests(unittest.TestCase):

    def _provisioned_rx(self):
        rx = _make_rx()
        rx._control = MagicMock()
        info = MagicMock()
        info.multicast_address = "239.1.2.3"
        info.port = 5004
        rx._control.ensure_channel.return_value = info
        return rx

    def _sink(self):
        sink = MagicMock()
        sink.frequency_hz = 28_145_000
        sink.preset = "usb"
        sink.sample_rate = 12000
        sink.encoding = 4
        return sink

    def test_add_sink_registers_channel_info_with_listener(self):
        """_add_sink_to_multi must register the SAME ChannelInfo object it
        hands the sink — in-place mutation by the listener is the whole
        mechanism (clients.md: 'assumes the listener mutates the same
        ChannelInfo object in place')."""
        mods = _fake_ka9q_modules()
        rx = self._provisioned_rx()
        ch_info = MagicMock()
        ch_info.ssrc = 42
        mods["ka9q"].MultiStream.return_value.add_channel.return_value = ch_info
        rx._status_listener = MagicMock()
        sink = self._sink()
        with mock.patch.dict(sys.modules, mods):
            rx._add_sink_to_multi(sink, {})
        sink.set_channel_info.assert_called_once_with(ch_info)
        rx._status_listener.register_channel.assert_called_once_with(ch_info)

    def test_register_failure_is_swallowed(self):
        mods = _fake_ka9q_modules()
        rx = self._provisioned_rx()
        mods["ka9q"].MultiStream.return_value.add_channel.return_value = (
            MagicMock(ssrc=7))
        rx._status_listener = MagicMock()
        rx._status_listener.register_channel.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, mods):
            rx._add_sink_to_multi(self._sink(), {})   # must not raise


class StopStopsListenerTests(unittest.TestCase):

    def test_stop_stops_listener_and_clears_it(self):
        rx = _make_rx()
        listener = MagicMock()
        rx._status_listener = listener
        rx.stop()
        listener.stop.assert_called_once_with()
        self.assertIsNone(rx._status_listener)

    def test_stop_swallows_listener_stop_error(self):
        rx = _make_rx()
        listener = MagicMock()
        listener.stop.side_effect = RuntimeError("already dead")
        rx._status_listener = listener
        rx.stop()   # must not raise
        self.assertIsNone(rx._status_listener)
