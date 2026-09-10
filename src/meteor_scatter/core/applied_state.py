"""The §3 ``timing_authority_applied`` report for one radiod instance.

CLIENT-CONTRACT §18.5 (amendment 2026-09-04): the field describes the
LABELS a client writes, not its reading habits.  An instance owns many
ChannelSinks, each pinned to its own :class:`hamsci_dsp.timing.AnchorUTC`;
the instance's labels ride the authority only when every anchored channel's
do.  A mixed state stays legal (§18.7) but must be visible, so it reports
null here — never an average — and the channel counts travel with the block
so the reader can see how many labels the report stands for.

The recorder writes the result once a minute to
``<spool>/<radiod_id>/timing-authority.json`` (hamsci_dsp.timing.
write_applied_state); ``meteor-scatter inventory --json`` reads it back.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from hamsci_dsp.timing import applied_state_for_anchors


def applied_state_for(
    sinks: Iterable,
    client_radiod: str,
    now_fn: Optional[Callable[[], float]] = None,
) -> Optional[dict]:
    """Aggregate the sinks' anchors into one §3 block, or None — the
    suite-shared rule in ``hamsci_dsp.timing.applied_state_for_anchors``
    (populated iff every anchored channel is corrected; counts attached)."""
    return applied_state_for_anchors(
        (getattr(s, "anchor", None) for s in sinks),
        client_radiod=client_radiod, now_fn=now_fn,
    )
