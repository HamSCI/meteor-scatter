"""The §3 ``timing_authority_applied`` report for one radiod instance.

One instance holds many ChannelSinks.  The instance's labels ride the
authority only when every anchored channel's do (CLIENT-CONTRACT §18.7: a
mixed state must be visible, never averaged away), so the aggregate is
populated iff at least one channel is anchored and none of the anchored
ones lack the correction.  The channel counts travel with it.
"""

import json
import unittest
from types import SimpleNamespace

from hamsci_dsp.timing import AnchorUTC

from meteor_scatter.core.applied_state import applied_state_for


def _anchor(offset_ns, source="rtp_to_utc+authority"):
    snap = SimpleNamespace(
        t_level_active="T6", sigma_ns=4210, governor_radiod="gov",
        utc_published=None, host_clock=None,
    ) if offset_ns is not None else None
    return AnchorUTC(
        utc=1_700_000_500.0, source=source,
        offset_seconds=(offset_ns or 0) / 1e9, offset_ns=offset_ns,
        snapshot=snap, rtp_referenced=True,
    )


def _sink(anchor):
    return SimpleNamespace(anchor=anchor)


class AppliedStateForTests(unittest.TestCase):

    def test_all_anchored_channels_corrected_reports_populated(self):
        sinks = [_sink(_anchor(4_250_000)), _sink(_anchor(4_250_000))]
        block = applied_state_for(sinks, client_radiod="rx")
        self.assertEqual(block["tier"], "T6")
        self.assertEqual(block["radiod_id"], "rx")
        self.assertEqual(block["channels"], {"total": 2, "anchored": 2, "applied": 2})

    def test_one_uncorrected_channel_makes_the_instance_report_null(self):
        sinks = [_sink(_anchor(4_250_000)), _sink(_anchor(None, source="rtp_to_utc"))]
        self.assertIsNone(applied_state_for(sinks, client_radiod="rx"))

    def test_nothing_anchored_yet_reports_null(self):
        sinks = [_sink(None), _sink(None)]
        self.assertIsNone(applied_state_for(sinks, client_radiod="rx"))

    def test_unanchored_channels_do_not_veto(self):
        # A channel still waiting for its first packet has no label to report.
        sinks = [_sink(_anchor(4_250_000)), _sink(None)]
        block = applied_state_for(sinks, client_radiod="rx")
        self.assertIsNotNone(block)
        self.assertEqual(block["channels"], {"total": 2, "anchored": 1, "applied": 1})

    def test_block_is_json_serialisable(self):
        block = applied_state_for([_sink(_anchor(1))], client_radiod="rx")
        json.dumps(block)
