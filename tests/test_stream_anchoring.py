"""Tests for ChannelSink's RTP-referenced SlotClock anchoring.

The recorder anchors a ka9q.SlotClock ONCE, off radiod's GPS-true RTP
timestamp (quality.last_rtp_timestamp) mapped to UTC via rtp_to_utc (or a
time.time() fallback when channel_info is absent).  Every batch is then
pushed to the ring keyed by its absolute RTP **sample offset** — never by a
delivered-sample-count UTC projection.  This is the fix for the
"decodes=N/N but spots=0" drift: ring offsets and slot boundaries are both
anchor-relative integer sample counts derived from the true RTP timestamp,
so the audio always lines up with the grid point its WAV is labelled with.
"""

import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import numpy as np

from meteor_scatter.core.stream import ChannelSink

SR = 12000


@dataclass
class _FakeQuality:
    """Stand-in for MultiStream's StreamQuality."""
    last_rtp_timestamp: int = 0
    total_samples_delivered: int = 0
    first_rtp_timestamp: int = 0


class _FakeChannelInfo:
    def __init__(self, gps_time=1462564880_000_000_000, rtp_timesnap=1):
        self.gps_time = gps_time
        self.rtp_timesnap = rtp_timesnap
        self.anchor_epoch = 0


class _NoAuthority:
    """AuthorityReader stub with no usable offset (standalone timing)."""
    def read(self):
        return None


def _make_sink(authority_reader=None) -> ChannelSink:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "msk144").mkdir(exist_ok=True)
    log_fd = open(tmp / "log", "ab")
    sink = ChannelSink(
        mode="msk144", frequency_hz=28_145_000, sample_rate=SR,
        preset="usb", encoding=0, spool_dir=tmp, log_fd=log_fd,
        decoder_path="/nonexistent", keep_wav=False,
        authority_reader=authority_reader,
    )
    sink._tmp_dir = tmp  # type: ignore[attr-defined]
    return sink


def _cleanup_sink(sink) -> None:
    tmp = getattr(sink, "_tmp_dir", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


class TestRtpAnchoring(unittest.TestCase):

    def test_first_batch_anchors_via_rtp_to_utc(self):
        # Inject a reader with no usable offset so the standalone anchor source
        # is asserted deterministically, regardless of whether this host
        # happens to have a live /run/hf-timestd/authority.json.
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            n = 2400
            q = _FakeQuality(last_rtp_timestamp=1_000_000 + n)
            with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
                with mock.patch("hamsci_dsp.timing.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push") as push:
                        sink.on_samples(np.zeros(n, dtype=np.float32), q)
            self.assertTrue(sink._clock.anchored)
            self.assertEqual(sink._anchor_source, "rtp_to_utc")
            self.assertAlmostEqual(sink._clock._anchor_utc, 1_700_000_500.0, places=3)
            # First batch's first sample is the anchor -> ring offset 0.
            push.assert_called_once()
            self.assertEqual(push.call_args[0][1], 0)
            # latest_rtp recorded = last packet ts.
            self.assertEqual(sink._latest_rtp, 1_000_000 + n)
        finally:
            _cleanup_sink(sink)

    def test_wall_clock_fallback_without_channel_info(self):
        # _NoAuthority + no channel_info -> the shared helper's bare wall-clock
        # fallback.  Mock the helper's own clock (hamsci_dsp.timing.time.time).
        sink = _make_sink(authority_reader=_NoAuthority())   # no channel_info set
        try:
            n = 2400          # 200 ms
            q = _FakeQuality(last_rtp_timestamp=500_000 + n)
            with mock.patch("hamsci_dsp.timing.time.time",
                            return_value=1_700_000_000.0):
                with mock.patch.object(sink._ring, "push"):
                    sink.on_samples(np.zeros(n, dtype=np.float32), q)
            self.assertEqual(sink._anchor_source, "wallclock_fallback")
            # anchor utc = now - n/sr
            self.assertAlmostEqual(sink._clock._anchor_utc,
                                   1_700_000_000.0 - n / SR, places=3)
        finally:
            _cleanup_sink(sink)

    def test_authority_offset_applied_to_anchor(self):
        class _FakeSnap:
            offset_usable = True
            offset_seconds = 0.004250
            rtp_to_utc_offset_ns = 4_250_000
        class _FakeReader:
            def read(self):
                return _FakeSnap()

        sink = _make_sink(authority_reader=_FakeReader())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            n = 2400
            q = _FakeQuality(last_rtp_timestamp=1_000_000 + n)
            with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
                with mock.patch("hamsci_dsp.timing.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(np.zeros(n, dtype=np.float32), q)
            self.assertEqual(sink._anchor_source, "rtp_to_utc+authority")
            self.assertAlmostEqual(sink._clock._anchor_utc,
                                   1_700_000_500.00425, places=4)
        finally:
            _cleanup_sink(sink)

    def test_ring_offsets_are_rtp_derived_not_wall_clock(self):
        """Offsets come from the true RTP timestamp; moving time.time()
        between batches must NOT shift them (the drift-immunity property)."""
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo())
        try:
            n1, n2 = SR, SR // 2     # 1.0 s then 0.5 s, contiguous in RTP
            r0 = 4_000_000
            with mock.patch("ka9q.rtp_to_utc", return_value=2000.0):
                with mock.patch.object(sink._ring, "push") as push:
                    with mock.patch("hamsci_dsp.timing.time.time",
                                    return_value=1000.0):
                        sink.on_samples(
                            np.zeros(n1, dtype=np.float32),
                            _FakeQuality(last_rtp_timestamp=r0 + n1))
                    # absurd wall-clock jump; RTP contiguous (next batch ends
                    # n2 later) -> offset must be exactly n1, unaffected.
                    with mock.patch("hamsci_dsp.timing.time.time",
                                    return_value=999_999_999.0):
                        sink.on_samples(
                            np.zeros(n2, dtype=np.float32),
                            _FakeQuality(last_rtp_timestamp=r0 + n1 + n2))
            self.assertEqual(push.call_args_list[0][0][1], 0)
            self.assertEqual(push.call_args_list[1][0][1], n1)
        finally:
            _cleanup_sink(sink)

    def test_on_stream_restored_resets_clock_and_ring(self):
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo())
        try:
            with mock.patch("ka9q.rtp_to_utc", return_value=2000.0):
                with mock.patch.object(sink._ring, "push"):
                    sink.on_samples(
                        np.zeros(SR, dtype=np.float32),
                        _FakeQuality(last_rtp_timestamp=1 + SR))
            self.assertTrue(sink._clock.anchored)

            new_info = _FakeChannelInfo(rtp_timesnap=999)
            with mock.patch.object(sink._ring, "clear") as clear:
                sink.on_stream_restored(new_info)
            # clock dropped, ring flushed, latest_rtp cleared, info replaced.
            self.assertFalse(sink._clock.anchored)
            clear.assert_called_once()
            self.assertIsNone(sink._latest_rtp)
            self.assertIs(sink._channel_info, new_info)
        finally:
            _cleanup_sink(sink)


class TestDesyncRecovery(unittest.TestCase):
    """audit F18: a SlotClockDesyncError from offset_of_rtp() must not
    propagate out of on_samples.  Recover the way SlotClock.advance()
    does internally (ka9q/slot_clock.py:250-264: log, reset, return) plus
    this sink's own on_stream_restored precedent: drop anchor AND ring
    (ring offsets live in the dead reference space), then re-anchor on
    the next batch."""

    def _anchor(self, sink, first_rtp=1_000_000, n=2400):
        q = _FakeQuality(last_rtp_timestamp=(first_rtp + n) & 0xFFFFFFFF)
        with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
            with mock.patch("hamsci_dsp.timing.time.time",
                            return_value=1_700_000_500.0):
                sink.on_samples(np.zeros(n, dtype=np.float32), q)
        self.assertTrue(sink._clock.anchored)

    def test_desync_batch_resets_instead_of_raising(self):
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            self._anchor(sink)
            n = 2400
            # > _SAFE_UNWRAP_SAMPLES (2**30) past the unwrap high-water:
            # offset_of_rtp cannot disambiguate and raises.
            far = (1_000_000 + n + 2**30 + 10 * SR) & 0xFFFFFFFF
            q2 = _FakeQuality(last_rtp_timestamp=far)
            sink.on_samples(np.zeros(n, dtype=np.float32), q2)  # must not raise
            self.assertFalse(sink._clock.anchored)
            self.assertIsNone(sink._anchor_rtp)
        finally:
            _cleanup_sink(sink)

    def test_next_batch_after_desync_reanchors(self):
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            self._anchor(sink)
            n = 2400
            far = (1_000_000 + n + 2**30 + 10 * SR) & 0xFFFFFFFF
            sink.on_samples(np.zeros(n, dtype=np.float32),
                            _FakeQuality(last_rtp_timestamp=far))
            # The very next batch re-establishes the grid cleanly.
            self._anchor(sink, first_rtp=far, n=n)
        finally:
            _cleanup_sink(sink)


if __name__ == "__main__":
    unittest.main()


class _FullSnap:
    """AuthoritySnapshot stand-in with the fields the §3 report needs."""
    offset_usable = True
    offset_seconds = 0.004250
    rtp_to_utc_offset_ns = 4_250_000
    t_level_active = "T6"
    sigma_ns = 4210
    governor_radiod = "AC0G-B4-status.local"
    utc_published = None
    host_clock = {"verdict": "ok"}


class _FullReader:
    def read(self):
        return _FullSnap()


class TestSlideFollowWrapHint(unittest.TestCase):
    """AC0G-B4 2026-09-08: the slide-follow re-pin mapped the FIXED anchor RTP
    with hint=now.  49.7 h after anchoring the wrap epoch flipped, the anchor's
    UTC jumped +99.42 h, and 136 FT8 spots were uploaded labelled four days
    ahead.  The hint for a fixed anchor must be the anchor's own UTC."""

    def test_repin_hints_with_the_anchor_utc_not_now(self):
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            n = 2400
            q = _FakeQuality(last_rtp_timestamp=1_000_000 + n)
            with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
                with mock.patch("hamsci_dsp.timing.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(np.zeros(n, dtype=np.float32), q)
            hints = []

            def _record(rtp, ci, wallclock_hint_sec=None):
                hints.append(wallclock_hint_sec)
                return 1_700_000_500.0
            # Five days later, "now" is more than P/2 past the anchor.
            with mock.patch("ka9q.rtp_to_utc", side_effect=_record):
                with mock.patch("hamsci_dsp.timing.time.time",
                                return_value=1_700_432_500.0):
                    sink._anchor_utc_now()
            self.assertEqual(len(hints), 1)
            self.assertAlmostEqual(hints[0], 1_700_000_500.0, places=3)
        finally:
            _cleanup_sink(sink)


class TestAnchorExposedForTheReport(unittest.TestCase):
    """The recorder reports what its labels ride (CLIENT-CONTRACT §18.5), so
    the sink keeps the AnchorUTC it pinned, not just a source string."""

    def test_sink_keeps_the_anchor_it_pinned(self):
        sink = _make_sink(authority_reader=_FullReader())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            self.assertIsNone(sink.anchor)
            n = 2400
            q = _FakeQuality(last_rtp_timestamp=1_000_000 + n)
            with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
                with mock.patch("hamsci_dsp.timing.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(np.zeros(n, dtype=np.float32), q)
            self.assertEqual(sink.anchor.offset_ns, 4_250_000)
            self.assertEqual(sink.anchor.source, "rtp_to_utc+authority")
            block = sink.anchor.timing_authority_applied(client_radiod="rx")
            self.assertEqual(block["tier"], "T6")
        finally:
            _cleanup_sink(sink)

    def test_reset_drops_the_anchor(self):
        sink = _make_sink(authority_reader=_FullReader())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            n = 2400
            q = _FakeQuality(last_rtp_timestamp=1_000_000 + n)
            with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
                with mock.patch.object(sink._ring, "push"):
                    sink.on_samples(np.zeros(n, dtype=np.float32), q)
            self.assertIsNotNone(sink.anchor)
            sink.on_stream_restored(_FakeChannelInfo(rtp_timesnap=9))
            self.assertIsNone(sink.anchor)
        finally:
            _cleanup_sink(sink)
