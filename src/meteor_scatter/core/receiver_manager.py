"""ReceiverManager: per-radiod channels, streams, and tailers.

One ReceiverManager per source (= one radiod control plane).  A
MeteorScatterRecorder process holds N of these — one for single-radiod
deployments (legacy ``--radiod-id`` mode) or several for multi-source
deployments where the same process drives a local radiod plus remote
radiods over the LAN (mirrors wspr-recorder's multi-source pattern).

The class owns everything radiod-specific:

  * ``RadiodControl`` connection
  * ``ChannelSink`` instances (one per (mode, freq))
  * ``MultiStream`` instances (grouped by multicast destination)
  * Lifetime-keepalive entries (handed to the process-global keep-alive
    thread in :mod:`recorder` so we don't spawn one thread per source)
  * Per-mode log file descriptors and ``ChTailer`` instances
  * Spool dir under ``<spool_root>/<radiod_id>``

MeteorScatterRecorder remains the process-global orchestrator: chrony settle gate,
uploader fanout, stats aggregation, main loop, and watchdog.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from meteor_scatter.config import (
    derive_source_key,
    get_freqs,
    get_mode_params,
    resolve_radiod_status,
)
from meteor_scatter.core.ch_tailer import ChTailer

# ChannelSink imports numpy via meteor_scatter.core.stream; defer the
# import so this module stays importable in lightweight test
# environments that don't carry numpy.  All annotation references go
# through the TYPE_CHECKING block (``from __future__ import
# annotations`` at the top of the file makes them lazy at runtime),
# and provision_channels does its own local import before
# instantiating sinks.
if TYPE_CHECKING:  # pragma: no cover
    from meteor_scatter.core.stream import ChannelSink

logger = logging.getLogger(__name__)

# Channel-verify softening.  radiod's control plane is slow to confirm a
# freshly-created channel while it is busy — a just-started (cold) RX888, or
# one already serving many peer-client channels (wspr-recorder's 17 bands +
# our 19).  The old behaviour used ka9q's 5 s hard verify and let the first
# slow channel's TimeoutError abort the whole daemon, which systemd then
# restart-looped — so a momentarily-busy radiod meant psk never came up at
# all.  Give each channel a generous verify budget, retry a few times, and
# treat a still-unverifiable channel as a skip (warn + continue) rather than
# a fatal error.  Both knobs are env-overridable for tuning without a
# redeploy.
_VERIFY_TIMEOUT_S = float(os.environ.get("METEOR_SCATTER_CHANNEL_VERIFY_TIMEOUT_S", "10"))
_VERIFY_RETRIES = int(os.environ.get("METEOR_SCATTER_CHANNEL_VERIFY_RETRIES", "1"))
_VERIFY_RETRY_BACKOFF_S = 1.5
# Total wall-clock budget for the whole provisioning sweep.  psk notifies
# systemd READY only AFTER provisioning every channel (recorder.py:_run),
# so the sweep must finish well inside the unit's TimeoutStartSec (3 min)
# or systemd SIGKILLs the daemon mid-provision.  Once the budget is spent
# we stop verifying and skip the remaining channels so the daemon still
# comes up (degraded) with whatever verified — the keepalive/refresh path
# can pick up stragglers later.
_VERIFY_BUDGET_S = float(os.environ.get("METEOR_SCATTER_CHANNEL_VERIFY_BUDGET_S", "120"))


def _resolve_encoding(enc_str: str) -> int:
    """Map config encoding string to ka9q.Encoding integer.

    Kept here (and re-exported from :mod:`recorder` for backwards
    compatibility) so ReceiverManager has no upward dependency on
    its caller.
    """
    mapping = {
        "s16be": 2,
        "s16le": 1,
        "f32": 4,
        "f32le": 4,
        "f32be": 8,
    }
    return mapping.get(enc_str.lower(), 2)


class ReceiverManager:
    """All radiod-specific state and lifecycle for one source.

    Construction is cheap (no I/O); ``provision_channels`` opens the
    control connection and creates channels.  Call ``start_streams`` +
    ``start_ch_tailers`` after provisioning.  ``stop`` is idempotent
    and safe to call from a signal handler.
    """

    def __init__(
        self,
        *,
        config: dict,
        radiod_block: dict,
        spool_root: Path,
        log_dir: Path,
        radiod_lifetime_frames: int,
        reporter_id: Optional[str] = None,
    ) -> None:
        self._config = config
        self._radiod = radiod_block
        # RADIOD-IDENTIFICATION.md §3.1: the mDNS multicast status name
        # IS the identifier.  No fallback — provision_channels would
        # fail without it anyway.
        self._radiod_id = resolve_radiod_status(radiod_block)
        self._reporter_id = reporter_id
        self._rx_source = f"radiod:{self._radiod_id}"
        self._spool_root = Path(spool_root) / self._radiod_id
        self._log_dir = Path(log_dir)
        self._radiod_lifetime_frames = int(radiod_lifetime_frames)

        self._control = None
        self._sinks: list[ChannelSink] = []
        self._multi_streams: list = []
        # (MultiStream, ssrc) pairs the process-global keepalive
        # thread refreshes.  MeteorScatterRecorder.start_lifetime_keepalive
        # gathers these across all ReceiverManagers.
        self._lifetime_entries: list[tuple[object, int]] = []
        self._ch_tailers: list[ChTailer] = []
        self._log_fds: dict[str, object] = {}

    # --- accessors used by MeteorScatterRecorder ---------------------------------

    @property
    def radiod_id(self) -> str:
        return self._radiod_id

    @property
    def rx_source(self) -> str:
        return self._rx_source

    @property
    def sinks(self) -> list[ChannelSink]:
        return self._sinks

    @property
    def lifetime_entries(self) -> list[tuple[object, int]]:
        return self._lifetime_entries

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    # --- provisioning --------------------------------------------------

    def provision_channels(
        self,
        *,
        decoder: str,
        decoder_kind: str,
        keep_wav: bool,
        spool_spots: bool,
    ) -> None:
        """Create ChannelSinks and register them with MultiStream(s).

        One MultiStream per unique multicast group, keyed on the
        (mcast_addr, port) returned by ensure_channel().  In the common
        case (FT8+FT4 share preset/sample_rate/encoding) all channels
        land on one group and we end up with a single MultiStream.
        """
        from ka9q import MultiStream, RadiodControl
        # Lazy: stream.py imports numpy.  See module docstring.
        from meteor_scatter.core.stream import ChannelSink

        status = resolve_radiod_status(self._radiod)
        # Re-derive the source key now that status is guaranteed
        # resolvable — env override may have appeared after __init__.
        self._rx_source = f"radiod:{status}"
        logger.info(
            "ReceiverManager %s: connecting to radiod at %s",
            self._radiod_id, status,
        )
        # client_id makes ka9q-python derive a per-(client, radiod)
        # multicast destination so this recorder's channels never share
        # a multicast group with peer clients on the same radiod
        # (wspr-recorder, hfdl-recorder, hf-timestd, etc.).  CONTRACT
        # v0.3 §7 / ka9q-python ≥ 3.14.0.
        self._control = RadiodControl(status, client_id="meteor-scatter")

        # Surface the Fusion governor identity at startup so the journal
        # record makes multi-radiod attribution clear.
        try:
            from hamsci_dsp.timing import AuthorityReader
            snap = AuthorityReader().read()
            governor = snap.governor_radiod if snap is not None else None
        except Exception as e:
            logger.debug("authority.json read at startup failed: %s", e)
            governor = None
        if governor and governor != status:
            logger.info(
                "Timing attribution: client_radiod=%s, "
                "fusion_governor_radiod=%s (differ — per-host clock-skew "
                "uncertainty applies to all spots)",
                status, governor,
            )
        elif governor:
            logger.info(
                "Timing attribution: client_radiod=fusion_governor_radiod=%s",
                status,
            )
        else:
            logger.info(
                "Timing attribution: client_radiod=%s, "
                "fusion_governor_radiod=<none> (hf-timestd authority not "
                "published — recorder will anchor via wall clock at "
                "stream start)",
                status,
            )

        multi_by_group: dict[tuple, object] = {}
        failed: list = []
        # Bound the whole sweep so READY is sent inside TimeoutStartSec.
        deadline = time.monotonic() + _VERIFY_BUDGET_S

        for mode in ("msk144",):
            freqs = get_freqs(self._radiod, mode)
            if not freqs:
                continue

            params = get_mode_params(self._radiod, mode)
            sample_rate = params["sample_rate"]
            preset = params["preset"]
            # The WAV writer (wav.py) peak-normalizes f32 -> s16be at
            # write time, so radiod MUST emit f32 on the wire: an s16
            # channel quantizes a low-level signal (e.g. a 25 dB-down FT8
            # channel) into the noise floor at the radiod before we ever
            # see it.  Force f32 regardless of the config `encoding` key.
            cfg_encoding = params.get("encoding", "f32")
            if cfg_encoding.lower() not in ("f32", "f32le"):
                logger.warning(
                    "ReceiverManager %s: %s config encoding=%s ignored; "
                    "forcing f32 (WAV writer requires float32 channel input)",
                    self._radiod_id, mode.upper(), cfg_encoding,
                )
            encoding_str = "f32"
            encoding_int = _resolve_encoding("f32")

            log_path = self._log_dir / f"{self._radiod_id}-{mode}.log"
            if mode not in self._log_fds:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                # Text mode (not binary) — Python str flows through.
                self._log_fds[mode] = open(log_path, "a", encoding="utf-8")

            for freq_hz in freqs:
                logger.info(
                    "ReceiverManager %s: provisioning %s %d Hz "
                    "(sr=%d, preset=%s, enc=%s)",
                    self._radiod_id, mode.upper(), freq_hz,
                    sample_rate, preset, encoding_str,
                )
                sink = ChannelSink(
                    mode=mode,
                    frequency_hz=freq_hz,
                    sample_rate=sample_rate,
                    preset=preset,
                    encoding=encoding_int,
                    spool_dir=self._spool_root,
                    log_fd=self._log_fds[mode],
                    decoder_path=decoder,
                    decoder_kind=decoder_kind,
                    keep_wav=keep_wav,
                    spool_spots=spool_spots,
                )
                if self._provision_one_with_retry(
                        sink, multi_by_group, deadline):
                    self._sinks.append(sink)
                else:
                    failed.append((mode, freq_hz))

        self._multi_streams = list(multi_by_group.values())
        logger.info(
            "ReceiverManager %s: provisioned %d channels across %d "
            "multicast group(s) on radiod %s",
            self._radiod_id, len(self._sinks),
            len(self._multi_streams), status,
        )
        if failed:
            logger.warning(
                "ReceiverManager %s: %d channel(s) could not be verified "
                "after %d attempt(s) and were skipped (radiod busy?): %s",
                self._radiod_id, len(failed), _VERIFY_RETRIES + 1,
                ", ".join(f"{m.upper()} {f} Hz" for m, f in failed),
            )
        if not self._sinks:
            raise RuntimeError(
                f"ReceiverManager {self._radiod_id}: no channels could be "
                f"provisioned on {status} ({len(failed)} verify attempt(s) "
                f"timed out — is radiod streaming?)"
            )

    def _provision_one_with_retry(self, sink, multi_by_group, deadline) -> bool:
        """Provision one channel, retrying on verify timeout within budget.

        Each attempt's verify timeout is capped to the time left in the
        sweep-wide ``deadline`` (monotonic).  Returns True once verified, or
        False if it could not be verified (retries exhausted, or the budget
        ran out) — the caller logs a summary and skips it instead of
        aborting the whole daemon.  A momentarily-busy radiod must neither
        take meteor-scatter down nor make it miss systemd's start deadline.
        """
        attempts = _VERIFY_RETRIES + 1
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                logger.warning(
                    "ReceiverManager %s: provisioning budget exhausted — "
                    "skipping %s %d Hz (and any later channels) without "
                    "verify so the daemon still comes up",
                    self._radiod_id, sink.mode.upper(), sink.frequency_hz,
                )
                return False
            attempt_timeout = min(_VERIFY_TIMEOUT_S, remaining)
            try:
                self._add_sink_to_multi(
                    sink, multi_by_group, timeout=attempt_timeout)
                if attempt > 1:
                    logger.info(
                        "ReceiverManager %s: %s %d Hz verified on attempt "
                        "%d/%d", self._radiod_id, sink.mode.upper(),
                        sink.frequency_hz, attempt, attempts,
                    )
                return True
            except TimeoutError as exc:
                can_retry = (
                    attempt < attempts
                    and (deadline - time.monotonic()
                         > _VERIFY_RETRY_BACKOFF_S + 1.0)
                )
                if can_retry:
                    logger.warning(
                        "ReceiverManager %s: %s %d Hz verify timed out "
                        "(attempt %d/%d): %s — retrying in %.1fs",
                        self._radiod_id, sink.mode.upper(),
                        sink.frequency_hz, attempt, attempts, exc,
                        _VERIFY_RETRY_BACKOFF_S,
                    )
                    time.sleep(_VERIFY_RETRY_BACKOFF_S)
                else:
                    logger.error(
                        "ReceiverManager %s: %s %d Hz could not be verified "
                        "(attempt %d/%d): %s — skipping",
                        self._radiod_id, sink.mode.upper(),
                        sink.frequency_hz, attempt, attempts, exc,
                    )
                    return False
        return False

    def _add_sink_to_multi(
        self, sink: ChannelSink, multi_by_group: dict,
        timeout: float = _VERIFY_TIMEOUT_S,
    ) -> None:
        """Attach sink to the MultiStream for its multicast group.

        Resolves the multicast group up-front via ensure_channel() so
        we can pick the right MultiStream (or create one) by
        (mcast_addr, port). MultiStream.add_channel() calls
        ensure_channel again internally — idempotent, one extra cheap
        status probe — but this keeps the grouping deterministic
        instead of relying on ValueError as control flow.
        """
        from ka9q import MultiStream

        # ``lifetime=None`` when configured to 0 — distinguishes "no
        # LIFETIME tag at all" (radiod template default applies) from
        # "finite N frames" (self-destruct timer enabled).  Phase A of
        # the WSPR fix proved the keepalive-vs-expiry race wedges
        # channels at Template defaults; same race exists here.
        lifetime_arg: Optional[int] = (
            self._radiod_lifetime_frames
            if self._radiod_lifetime_frames > 0 else None
        )

        info = self._control.ensure_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            lifetime=lifetime_arg,
            timeout=timeout,
        )
        key = (info.multicast_address, info.port)
        multi = multi_by_group.get(key)
        if multi is None:
            multi = MultiStream(control=self._control)
            multi_by_group[key] = multi

        ch_info = multi.add_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            on_samples=sink.on_samples,
            on_stream_dropped=sink.on_stream_dropped,
            on_stream_restored=sink.on_stream_restored,
            lifetime=lifetime_arg,
            timeout=timeout,
        )
        # Hand the freshly-discovered ChannelInfo (with GPS_TIME /
        # RTP_TIMESNAP / chain_delay_correction populated) to the sink
        # so on_samples can use rtp_to_utc as the UTC anchor source.
        sink.set_channel_info(ch_info)
        if lifetime_arg is not None:
            self._lifetime_entries.append((multi, ch_info.ssrc))

    def start_streams(self) -> None:
        for sink in self._sinks:
            try:
                sink.start()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: failed to start sink %s %d Hz",
                    self._radiod_id, sink.mode, sink.frequency_hz,
                )
        for multi in self._multi_streams:
            try:
                multi.start()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: failed to start MultiStream",
                    self._radiod_id,
                )

    def start_ch_tailers(
        self,
        *,
        callsign: str,
        host_grid: str,
        proc_version: str,
        forward_flag: bool,
        cycle_batcher: Optional[object] = None,
    ) -> None:
        """Start one ChTailer per (radiod, mode) — CONTRACT v0.6 §17.

        Each tailer watches the ``<radiod_id>-msk144.log`` file that
        slot.py writes normalized jt9 decodes into, parses new lines, and
        forwards rows to ``cycle_batcher`` (cycle-aligned commit, log line
        in WSPR-parity format, foundation for cross-rx dedup).  When
        ``cycle_batcher`` is None the tailer falls back to the direct-to-
        sink Writer — kept so tests don't have to spin up a batcher.
        Rows land in the SQLite sink (``msk144.spots``); external upload
        is deferred (Phase 3).
        """
        callhash_path = self._spool_root / "callhash.json"
        for tailer_mode in ("msk144",):
            if not get_freqs(self._radiod, tailer_mode):
                continue
            log_path = self._log_dir / f"{self._radiod_id}-{tailer_mode}.log"
            try:
                tailer = ChTailer(
                    log_path=log_path,
                    mode=tailer_mode,
                    radiod_id=self._radiod_id,
                    reporter_id=self._reporter_id,
                    rx_source=self._rx_source,
                    host_call=callsign,
                    host_grid=host_grid,
                    processing_version=proc_version,
                    callhash_path=callhash_path,
                    forward_to_pskreporter=forward_flag,
                    cycle_batcher=cycle_batcher,
                )
                tailer.start()
                self._ch_tailers.append(tailer)
            except Exception:
                logger.exception(
                    "ReceiverManager %s: ch_tailer %s startup failed",
                    self._radiod_id, tailer_mode,
                )

    # --- shutdown ------------------------------------------------------

    def stop(self) -> None:
        """Idempotent shutdown — safe to call multiple times."""
        for tailer in self._ch_tailers:
            try:
                tailer.stop()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: error stopping ch_tailer",
                    self._radiod_id,
                )
        self._ch_tailers.clear()

        for multi in self._multi_streams:
            try:
                multi.stop()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: error stopping MultiStream",
                    self._radiod_id,
                )
        self._multi_streams.clear()

        for sink in self._sinks:
            try:
                sink.stop()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: error stopping sink",
                    self._radiod_id,
                )
        # Sinks intentionally kept in self._sinks so a final
        # stats_snapshot in MeteorScatterRecorder's stats thread can still
        # read them.

        for fd in self._log_fds.values():
            try:
                fd.close()
            except Exception:
                pass
        self._log_fds.clear()

        if self._control is not None:
            try:
                self._control.close()
            except Exception:
                pass
            self._control = None
