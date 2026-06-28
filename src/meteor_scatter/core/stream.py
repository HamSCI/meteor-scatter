"""ChannelSink: per-channel Ring + SlotWorker driven by MultiStream callbacks.

One ChannelSink per (mode, frequency). The sink owns no socket and no
thread of its own for RTP reception — it receives sample batches via
the `on_samples` callback that a shared `MultiStream` dispatches after
demultiplexing by SSRC.

Timing model (RTP-referenced ka9q.SlotClock — anchor-once + re-validate).

  1. On the FIRST on_samples batch we anchor a shared ``ka9q.SlotClock``
     off radiod's GPS-true RTP timestamp (``quality.last_rtp_timestamp``)
     mapped to UTC via the suite-shared ``hamsci_dsp.timing.acquire_anchor_utc``
     helper.  Preferred source: ``ka9q.rtp_to_utc`` (radiod's GPS_TIME /
     RTP_TIMESNAP snapshot) plus hf-timestd's §18 dynamic RTP→UTC offset.
     Fallback when channel_info is unavailable: the host wall clock.

  2. Every subsequent batch is pushed to the ring keyed by its absolute
     RTP **sample offset** (``clock.offset_of_rtp(batch_first_rtp)``) —
     NOT by a delivered-sample-count UTC projection.  The audio handed to
     jt9 therefore always lines up with the RTP grid point its WAV is
     labelled with, which removes the long-standing "decodes=N/N but
     spots=0" drift surface entirely.

  3. A throttled (~1 Hz) ``_maybe_revalidate`` recomputes the next
     boundary's true UTC from the StatusListener-refreshed ``channel_info``
     and compares it to the SlotClock's grid projection; a sustained gross
     divergence means the INITIAL anchor was wrong (stale GPS snapshot /
     wrap mis-disambiguation) — re-anchor off the fresh reference and flush
     the ring (whose offsets are anchor-relative).

Per METROLOGY.md §4.5 RTP-reference invariant, the recorder does not
diagnose timing health on its own — that is hf-timestd's job.  If the
host clock is badly wrong at anchor time, decode rate goes to zero and
the operator sees the symptom through the standard decode-health signal
(decodes_ok/decodes_total + sigmond's wav_snapshot), not through any
client-side wall-clock comparison.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from ka9q import SlotClock

from meteor_scatter.config import MSK144_CADENCE_SEC
from hamsci_dsp.timing import AuthorityReader
from meteor_scatter.core.ring import Ring
from meteor_scatter.core.slot import SlotWorker, SETTLE_SEC

logger = logging.getLogger(__name__)

RING_SECONDS = 60.0


class ChannelSink:
    """Ring + SlotWorker for one channel, fed by MultiStream callbacks."""

    def __init__(
        self,
        mode: str,
        frequency_hz: int,
        sample_rate: int,
        preset: str,
        encoding: int,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        keep_wav: bool = False,
        authority_reader: Optional[AuthorityReader] = None,
        decoder_kind: str = "jt9",
        spool_spots: bool = False,
        radiod_id: str = "",
        fault_reporter=None,
        fault_threshold_sec: float = 0.35,
    ):
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._sample_rate = sample_rate
        self._preset = preset
        self._encoding = encoding

        cadence = MSK144_CADENCE_SEC

        self._ring = Ring(
            max_seconds=RING_SECONDS,
            sample_rate=sample_rate,
        )

        # Epoch-aligned, RTP-referenced slot timing.  The clock is anchored
        # off radiod's GPS-true RTP timestamp (on_samples) and harvested by
        # the SlotWorker thread; the lock guards the shared clock state.
        self._clock = SlotClock(
            cadence_sec=cadence, sample_rate=sample_rate, settle_sec=SETTLE_SEC,
        )
        self._clock_lock = threading.Lock()
        # RTP timestamp just past the newest delivered sample (the clock's
        # high-water mark).  Written by on_samples, read by the SlotWorker.
        self._latest_rtp: Optional[int] = None

        self._slot_worker = SlotWorker(
            ring=self._ring,
            mode=mode,
            frequency_hz=frequency_hz,
            cadence_sec=cadence,
            spool_dir=spool_dir / mode,
            log_fd=log_fd,
            decoder_path=decoder_path,
            clock=self._clock,
            get_latest_rtp=lambda: self._latest_rtp,
            clock_lock=self._clock_lock,
            decoder_kind=decoder_kind,
            keep_wav=keep_wav,
            spool_spots=spool_spots,
        )

        self._total_delivered: int = 0
        # ChannelInfo carrying gps_time / rtp_timesnap / chain_delay — used to
        # map RTP→UTC at anchor time and, kept fresh by the StatusListener, to
        # RTP-reference re-validate the SlotClock anchor (_maybe_revalidate).
        self._channel_info = None
        # Diagnostic: how the current SlotClock anchor was derived.
        self._anchor_source: str = ""        # "rtp_to_utc[+authority]" | "wallclock_fallback"
        # ChannelInfo.anchor_epoch observed when the anchor was set (diag).
        self._anchor_epoch: Optional[int] = None
        # §18 authority reader — supplies the dynamic RTP→UTC offset at anchor
        # time (0.0 standalone); also inspectable by diags.
        self._reader = authority_reader if authority_reader is not None else AuthorityReader()
        # RTP-reference re-validation state (see _maybe_revalidate).  The
        # StatusListener keeps self._channel_info's (gps_time, rtp_timesnap)
        # fresh, so re-anchoring lands on radiod's current GPS reference.
        self._radiod_id = radiod_id
        self._fault_reporter = fault_reporter
        # Threshold + persistence env-tunable.  0.35 s sits above the ~0.28 s
        # post-restart anchor-settling and the ~0.45 s status-anchor jitter
        # cadence; requiring _fault_after consecutive ~1 Hz over-threshold
        # checks filters a lone jitter spike while still catching a sustained
        # real divergence.
        self._fault_threshold_sec = float(
            os.environ.get("METEOR_SCATTER_TIMING_FAULT_SEC",
                           str(fault_threshold_sec)))
        self._fault_after = max(1, int(
            os.environ.get("METEOR_SCATTER_TIMING_FAULT_AFTER", "2")))
        self._fault_strikes = 0
        self._last_check_mono = 0.0
        self._last_reanchor_mono = 0.0
        self._reanchor_cooldown_sec = 30.0

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def frequency_hz(self) -> int:
        return self._frequency_hz

    def stats_snapshot(self) -> dict:
        sw = self._slot_worker
        return {
            "mode": self._mode,
            "freq": self._frequency_hz,
            "decodes_ok": sw.decodes_ok,
            "decodes_fail": sw.decodes_fail,
            "slots_empty": sw.slots_empty,
        }

    def start(self) -> None:
        self._slot_worker.start()
        logger.info(
            "%s %d Hz: sink started (sr=%d)",
            self._mode.upper(), self._frequency_hz, self._sample_rate,
        )

    def stop(self) -> None:
        self._slot_worker.stop()
        logger.info(
            "%s %d Hz: sink stopped (total_delivered=%d)",
            self._mode.upper(), self._frequency_hz, self._total_delivered,
        )

    def set_channel_info(self, channel_info) -> None:
        """Attach the ChannelInfo carrying gps_time/rtp_timesnap/chain_delay.

        Called by the recorder right after multi.add_channel() returns.
        Without it, on_samples falls back to wall-clock anchoring (the
        old broken path) and logs a one-time warning per channel.
        """
        self._channel_info = channel_info

    def on_samples(self, samples: np.ndarray, quality) -> None:
        """MultiStream callback — feed the RTP-referenced SlotClock + ring.

        Each batch is tagged with the absolute sample offset of its FIRST
        sample, derived from radiod's GPS-true RTP timestamp
        (``quality.last_rtp_timestamp``) via the shared ``ka9q.SlotClock`` —
        NOT from a delivered-sample-count projection.  This is the fix for
        the long-standing "decodes=N/N but spots=0" drift (the audio handed
        to the decoder now always lines up with the RTP grid point its WAV
        is labelled with).  The slot harvesting + WAV write happen on the
        SlotWorker thread; here we only anchor, push, and re-validate.
        """
        n = len(samples)
        if n == 0:
            return
        last_rtp = getattr(quality, "last_rtp_timestamp", None)
        if not last_rtp:
            # No RTP timestamp yet (pre-first-packet) — nothing to anchor to.
            return
        last_rtp = int(last_rtp) & 0xFFFFFFFF
        # RTP timestamp of this batch's first sample.  The batch ends ~at the
        # last packet's timestamp; first sample = last_rtp - n.  The <1-packet
        # constant bias from ignoring the final packet's own length is
        # harmless (jt9 --msk144 tolerates the slot's settle window) and does
        # NOT accumulate — every batch is pinned to a true GPS-stamped RTP
        # value.
        batch_first_rtp = (last_rtp - n) & 0xFFFFFFFF

        with self._clock_lock:
            if not self._clock.anchored:
                anchor_utc, source = self._anchor_utc_for(batch_first_rtp, n)
                if anchor_utc is None:
                    return
                self._clock.anchor(batch_first_rtp, anchor_utc)
                self._anchor_source = source
                self._anchor_epoch = getattr(
                    self._channel_info, "anchor_epoch", None)
                logger.info(
                    "%s %d Hz: SlotClock anchored via %s",
                    self._mode.upper(), self._frequency_hz, source,
                )
            start_off = self._clock.offset_of_rtp(batch_first_rtp)

        self._ring.push(samples, start_off)
        self._latest_rtp = last_rtp
        self._total_delivered += n

        self._maybe_revalidate()

    def _maybe_revalidate(self) -> None:
        """Throttled RTP-reference anchor check (~1 Hz).

        Recompute the next boundary's true UTC from the StatusListener-
        refreshed ``channel_info`` via ``rtp_to_utc`` and compare to the
        SlotClock's grid projection.  A sustained gross divergence means the
        INITIAL anchor was wrong (stale GPS snapshot / wrap mis-
        disambiguation) — re-anchor off the fresh reference and flush the
        ring (whose offsets are anchor-relative).  Cheap and rare; the grid
        itself never drifts, so this only ever fires on a bad anchor.
        """
        if self._channel_info is None:
            return
        mono = time.monotonic()
        if mono - self._last_check_mono < 1.0:
            return
        self._last_check_mono = mono
        try:
            from ka9q import rtp_to_utc
            with self._clock_lock:
                div = self._clock.divergence_sec(
                    self._channel_info, rtp_to_utc)
        except Exception as exc:  # noqa: BLE001 — detection must not crash audio
            logger.debug("%s %d Hz: divergence check raised: %s",
                         self._mode.upper(), self._frequency_hz, exc)
            return
        if div is None:
            return
        if abs(div) <= self._fault_threshold_sec:
            self._fault_strikes = 0
            return
        self._fault_strikes += 1
        if self._fault_strikes < self._fault_after:
            return
        self._fault_strikes = 0
        if mono - self._last_reanchor_mono < self._reanchor_cooldown_sec:
            return
        self._last_reanchor_mono = mono
        if self._fault_reporter is not None:
            try:
                self._fault_reporter.report(
                    self._mode, self._frequency_hz, div)
            except Exception:  # noqa: BLE001
                pass
        logger.error(
            "TIMING FAULT rx=%s mode=%s %d Hz: SlotClock anchor diverged "
            "%+.3fs from radiod GPS reference — re-anchoring + flushing ring",
            self._radiod_id, self._mode, self._frequency_hz, div,
        )
        with self._clock_lock:
            self._clock.reset()
        self._ring.clear()
        self._latest_rtp = None

    def _anchor_utc_for(self, rtp_ts: int, n: int):
        """Return (utc, source) mapping ``rtp_ts`` -> UTC via the suite-shared
        anchor helper.

        Preferred: radiod's GPS/RTP timebase (``ka9q.rtp_to_utc``) plus the
        hf-timestd §18 dynamic RTP→UTC offset.  Fallback: the host wall clock
        naming this batch's first sample (``n`` samples back).  The logic lives
        once in ``hamsci_dsp.timing.acquire_anchor_utc`` so every sigmond
        recorder anchors identically — no per-client copies to drift.
        """
        from ka9q import rtp_to_utc
        from hamsci_dsp.timing import acquire_anchor_utc
        a = acquire_anchor_utc(
            first_rtp=rtp_ts,
            channel_info=self._channel_info,
            rtp_to_utc=rtp_to_utc,
            authority_reader=self._reader,
            samples_behind=n,
            sample_rate=self._sample_rate,
        )
        return a.utc, a.source

    def on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )

    def on_stream_restored(self, channel_info) -> None:
        # Re-anchor on stream restoration.  MultiStream only fires this
        # callback after _drop_timeout_sec (default 15s) of silence AND
        # a successful ensure_channel() — i.e. a real radiod restart or
        # comparable outage, never a sub-second multicast hiccup.  On
        # such a restart, MultiStream resets ``slot.quality =
        # StreamQuality()``, so the RTP timestamps restart from radiod's
        # fresh epoch.  Holding the pre-restart anchor across that
        # discontinuity makes every projected offset miss every slot
        # window, and decodes silently fall to 0/0 forever (observed
        # B4-100 2026-05-14: radiod bounced, every band silent for 3 h
        # until manual stop+start).  Re-anchoring is the intended
        # behavior.
        self._channel_info = channel_info
        with self._clock_lock:
            self._clock.reset()
        self._ring.clear()
        self._latest_rtp = None
        self._anchor_source = ""
        logger.info(
            "%s %d Hz: stream restored — re-anchoring on next batch",
            self._mode.upper(), self._frequency_hz,
        )

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def encoding(self) -> int:
        return self._encoding
