"""SlotWorker: extracts cadence-aligned WAV slots and invokes the decoder.

One SlotWorker per channel. Runs as a daemon thread, polling the ring
buffer every 500 ms for completed slots.

Slot-boundary timing now lives in ka9q.SlotClock (see ka9q-python
tests/test_slot_clock.py).  The clock is anchored by ChannelSink.on_samples
off radiod's GPS-true RTP timestamp; this worker only harvests completed
slots (``clock.advance(latest_rtp)``) and extracts their exact sample window
from the ring by absolute RTP offset — immune to the delivered-sample-count
drift the old UTC-projection cadence math suffered.

The decoder backend (selected by ``decoder_kind``):

  * ``"jt9"`` — WSJT-X's ``jt9`` in MSK144 mode (``jt9 --msk144 -p <tr>``,
    where ``<tr>`` is the configured T/R period = the slot cadence).
    Unlike decode_ft8, jt9 writes its decodes to a ``decoded.txt`` file
    in its ``-a`` data dir (a stable per-channel workdir) and prints only
    a ``<DecodeFinished>`` sentinel to stdout.  After each run we read the
    lines appended to ``decoded.txt`` since the previous run, normalize
    them (see ``core.decoder.normalize_log_line``), and append to the
    per-mode log.

The normalized lines land in ``<radiod>-msk144.log``;
``ch_tailer.parse_decoder_line`` parses each one into a spot row.

Recordings: every slot's WAV lands in the spool.  A slot jt9 decoded something
in goes to the MSQP archive (core/msqp.py) with a JSON sidecar; every other
slot, and any recording the archive could not take, is deleted.

capture = "iq": the ring holds complex64 I/Q.  Each slot's I/Q goes to the spool as
``<stem>.iq.wav`` (two-channel F32, exactly as received), and the audio jt9 reads is made from
it (core/iq.py) and written as ``<stem>.wav``.  On a decode the ``.iq.wav`` is what the archive
keeps, under the name ``<stem>.wav``; the audio file goes.  Otherwise both go.  NaN or inf in a
slot never reaches jt9, the gain or the noise floor (core/wav.py), and logs once per slot.

MSK144 cadence: T/R slots at the configured tr_period_sec (default 15 s, the
WSJT-X User Guide's MSK144 period — slots at :00/:15/:30/:45; 30 s gives :00, :30).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ka9q import SlotClock, SlotClockDesyncError

from meteor_scatter.config import CAPTURE_MODES
from meteor_scatter.core import decoder as _decoder
from meteor_scatter.core.iq import iq_to_usb_audio, write_iq_wav
from meteor_scatter.core.ring import Ring
from meteor_scatter.core.wav import WavStats, write_wav, write_wav_f32

logger = logging.getLogger(__name__)

SETTLE_SEC = 1.5

# A hung jt9 (e.g. on a corrupt WAV) would otherwise sit in
# _pending_procs forever, leaking its two stdio FDs + the spool WAV.
# jt9 --msk144 finishes in well under a second on a 15 s slot, so any
# proc still alive after this deadline is killed.  Generous (4x the
# cadence) to avoid false kills under top-of-minute CPU contention.
DECODE_TIMEOUT_SEC = 60.0

# decoder_kind values accepted by SlotWorker.
DECODER_JT9 = "jt9"
VALID_DECODER_KINDS = (DECODER_JT9,)


def inflight_from(pending_procs, now_mono: float) -> tuple[int, float]:
    """Pure core of SlotWorker.inflight_snapshot (tested without a worker).

    Each pending entry is (proc, wav_path, slot_start_utc, fork_monotonic);
    a decoder's age is measured from its fork.
    """
    ages = [now_mono - entry[3] for entry in pending_procs]
    return len(ages), (max(ages) if ages else 0.0)


TIMING_NOTES = [
    "utc(sample i of this recording) = label_anchor_utc + (first_sample_offset + i) / sample_rate; "
    "this reproduces the label the slot was cut by, to one sample.",
    "label_anchor_utc is the UTC of anchor_rtp as radiod's RTP-to-UTC mapping stood when the slot "
    "was cut; anchor.utc is the same RTP sample's UTC at the original pin, so "
    "label_anchor_utc - anchor.utc is the slide since the stream started.",
    "first_sample_rtp = (anchor_rtp + first_sample_offset) mod 2**32, the RTP header's wrapped "
    "timestamp of sample 0.",
    "slot_start_utc_exact is the requested slot boundary; it lies within half a sample of the "
    "UTC of sample 0.",
    "batch_rtp_basis names how each delivered batch's first sample got its RTP timestamp, and so "
    "how far these labels can be trusted.  'delivered_rtp_start': ka9q-python's resequencer "
    "reported it (gap fills included); the labels hold to a sample.  'last_packet_minus_n': an "
    "older ka9q-python gave only the last received packet's header, and the label is that minus "
    "the batch length, which runs one packet early (each sample's true RTP and UTC lie one "
    "packet later than the formulas above give: about 20 ms if radiod sends a 20 ms frame per "
    "packet), more under loss.  'mixed': the basis changed while this anchor held, so the "
    "labels are not consistent.  null: no basis was recorded.",
]


def sweep_spool(spool_dir: Path, max_age_sec: float, now: Optional[float] = None) -> int:
    """Remove ``*.wav``, ``*.iq.wav`` and ``*.part`` older than ``max_age_sec`` from the spool.

    A crash or kill leaves slot files that no worker owns.  The sweep runs once at startup, over
    the spool directory itself (not the ``work_*`` dirs), and returns how many files it removed.
    """
    now = time.time() if now is None else now
    removed = 0
    for pattern in ("*.wav", "*.iq.wav", "*.part"):
        for f in Path(spool_dir).glob(pattern):
            try:
                if f.is_file() and now - f.stat().st_mtime > max_age_sec:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


class SlotWorker:
    """Extracts cadence-aligned audio slots from a Ring and decodes them."""

    def __init__(
        self,
        ring: Ring,
        mode: str,
        frequency_hz: int,
        cadence_sec: float,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        clock: SlotClock,
        get_latest_rtp: Callable[[], Optional[int]],
        clock_lock: threading.Lock,
        get_anchor_utc_now: Callable[[], Optional[float]],
        msqp=None,
        decoder_kind: str = DECODER_JT9,
        spool_spots: bool = False,
        on_desync: Optional[Callable[[], None]] = None,
        capture: str = "usb",
        get_anchor: Optional[Callable[[], object]] = None,
        client_radiod: Optional[str] = None,
        get_rtp_basis: Optional[Callable[[], Optional[str]]] = None,
    ):
        if capture not in CAPTURE_MODES:
            raise ValueError(f"capture must be one of {CAPTURE_MODES}; got {capture!r}")
        if decoder_kind not in VALID_DECODER_KINDS:
            raise ValueError(
                f"decoder_kind must be one of {VALID_DECODER_KINDS}; "
                f"got {decoder_kind!r}"
            )
        self._ring = ring
        self._capture = capture
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._cadence_sec = cadence_sec
        self._spool_dir = spool_dir
        self._log_fd = log_fd
        # Resolve the jt9 binary once (arch-specific bundled binary, or an
        # explicit override).  An empty decoder_path means "auto-resolve".
        self._decoder_path = _decoder.resolve_jt9_binary(decoder_path)
        # F32 audio only in I/Q capture, and only for a jt9 that reads it (our port).  The usb
        # path writes 16-bit audio whatever jt9 is installed: it is the proof's control arm,
        # and its archive promises 16-bit WAVs that WSJT-X 3.0.2 opens.
        reads_f32 = _decoder.jt9_reads_f32(self._decoder_path)
        self._audio_f32 = capture == "iq" and reads_f32
        if self._audio_f32:
            why = "lists --msk-shmsg: the port reads float WAV"
        elif capture != "iq":
            why = "capture=usb keeps 16-bit audio whatever jt9 is installed"
        else:
            why = "does not list --msk-shmsg, or its -h probe failed: WSJT-X reads 16-bit only"
        logger.info(
            "%s %d Hz: capture=%s, jt9 audio is %s (%s %s)",
            mode.upper(), frequency_hz, capture,
            "F32" if self._audio_f32 else "16-bit",
            self._decoder_path, why,
        )
        # Epoch-aligned, RTP-referenced slot timing (shared ka9q.SlotClock).
        # The clock is anchored by ChannelSink.on_samples off the GPS-true RTP
        # timestamp; this worker only harvests completed slots and extracts
        # their exact sample windows by absolute offset.
        self._clock = clock
        self._get_latest_rtp = get_latest_rtp
        self._clock_lock = clock_lock
        # Returns the CURRENT UTC of the (fixed) SlotClock anchor_rtp per
        # radiod's live rtp_to_utc + authority offset.  We re-pin every slot's
        # RTP window to this each tick, so the windows follow radiod's slow
        # RTP↔UTC slide instead of freezing — without the per-batch re-anchor
        # storm (this is a smooth, sub-sample nudge once the grid is running).
        self._get_anchor_utc_now = get_anchor_utc_now
        # audit F18: invoked when offset_of_rtp() raises
        # SlotClockDesyncError during harvest — ChannelSink._reset_timing,
        # the same full anchor+ring reset used on stream restore.
        self._on_desync = on_desync
        # Returns the hamsci_dsp.timing.AnchorUTC the slot labels ride (None before anchoring);
        # each slot's sidecar timing block is built from it at the moment the slot is cut.
        self._get_anchor = get_anchor
        self._client_radiod = client_radiod
        # Returns how the sink labels each batch's first sample (ChannelSink.rtp_basis).
        self._get_rtp_basis = get_rtp_basis
        self._slot_timing: dict[Path, dict] = {}
        self._anchor_rtp_missing = False
        # Conditions that last across slots: key -> [slots so far, samples so far].
        self._episodes: dict[str, list[int]] = {}
        # Next clean cadence-multiple UTC boundary to emit (None until first).
        self._next_boundary_utc: Optional[float] = None
        self._sr = clock.sample_rate
        self._decoder_kind = decoder_kind
        # MsqpChannel (core/msqp.py) or None: None deletes every recording after decode.
        self._msqp = msqp
        # MSQP faults that escaped MsqpChannel's own guards: counted, and logged once on the
        # change and once on recovery, so a persistent fault never writes a line per slot.
        self.msqp_errors = 0
        self._msqp_faulty: dict[str, bool] = {}
        # What write_wav did to each spool WAV still awaiting its decode.
        self._slot_stats: dict[Path, WavStats] = {}
        self._spool_spots = spool_spots
        # Stable per-channel jt9 data dir — jt9 keeps its FFTW wisdom and
        # scratch files (decoded.txt, timer.out) here across slots.  NOTE:
        # jt9 --msk144 writes its decodes to STDOUT, not decoded.txt (that
        # file stays empty); we harvest each slot's decodes from the
        # process's stdout in _reap.  Per-frequency so the two bands never
        # share a workdir.
        freq_khz = frequency_hz // 1000
        self._workdir = Path(spool_dir) / f"work_{freq_khz}"
        _decoder.ensure_workdir(self._workdir)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Each entry: (proc, wav_path, slot_start_utc, fork_monotonic).
        self._pending_procs: list[tuple[subprocess.Popen, Path,
                                        float, float, int]] = []
        # Counters read by the recorder's stats thread. int ops are atomic
        # under CPython GIL; no lock needed for the single-reader case.
        self.decodes_ok = 0
        self.decodes_fail = 0
        self._jt9_usage_seen = False   # one ERROR per episode, not per slot
        self.slots_empty = 0

    def inflight_snapshot(self) -> tuple[int, float]:
        """(jt9 decodes not yet reaped, age of the oldest in seconds).

        The decode-backlog signal (see core/backlog.py): decoders from an
        earlier slot still running when the next slot fires means this
        channel is not keeping up.
        """
        return inflight_from(self._pending_procs, time.monotonic())

    def reset_boundary(self) -> None:
        """Drop the cached next-boundary UTC so the worker re-seeds at the new
        leading edge.  Called by ChannelSink.on_stream_restored after a genuine
        radiod restart re-anchors the clock to a fresh RTP reference."""
        self._next_boundary_utc = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"slot-{self._mode}-{self._frequency_hz}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self._reap_all(wait=True)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("SlotWorker tick error")
            time.sleep(0.5)

    def _tick(self) -> None:
        self._reap_finished()

        latest_rtp = self._get_latest_rtp()
        if latest_rtp is None:
            return
        # Current UTC of the FIXED anchor_rtp, per radiod's live rtp_to_utc.
        # This is what lets the grid FOLLOW radiod's RTP↔UTC slide: anchor_rtp
        # never moves (so ring offsets stay valid), but its UTC — and thus the
        # RTP offset of each clean cadence boundary — tracks radiod every tick.
        # SlotClock has no public reading of its anchor RTP, so the guard below reads the
        # private _anchor_rtp.  If a ka9q-python update renames it, no slot can be cut: say so
        # once, loudly, rather than stop decoding in silence.
        if not hasattr(self._clock, "_anchor_rtp"):
            if not self._anchor_rtp_missing:
                self._anchor_rtp_missing = True
                logger.error(
                    "%s %d Hz: ka9q SlotClock has no _anchor_rtp attribute (ka9q-python "
                    "changed?); the harvest guard cannot check the anchor, so NO slots are "
                    "cut or decoded; logged once",
                    self._mode.upper(), self._frequency_hz)
            return
        anchor_rtp_seen = self._clock._anchor_rtp
        anchor_utc_now = self._get_anchor_utc_now()
        if anchor_utc_now is None:
            return

        cadence_samples = self._clock.cadence_samples
        settle_samples = self._clock.settle_samples
        harvested: list[tuple[int, float, Optional[dict]]] = []
        try:
            with self._clock_lock:
                if not self._clock.anchored:
                    return
                # anchor_utc_now was computed above, outside the lock, for whichever anchor_rtp
                # was current then.  A re-anchor in between would pair that UTC with the new
                # anchor_rtp, so the harvest waits for the next tick.
                if self._clock.rtp_of_offset(0) != anchor_rtp_seen:
                    return
                latest_off = self._clock.offset_of_rtp(latest_rtp)
                # Seed the next boundary at the first clean cadence multiple at/after
                # the STREAM START (anchor_rtp is the first sample, so anchor_utc_now
                # ~ the stream-start UTC).  A stream that starts mid-slot correctly
                # begins at the next clean boundary, skipping the partial slot.
                if self._next_boundary_utc is None:
                    self._next_boundary_utc = (
                        math.ceil(anchor_utc_now / self._cadence_sec) * self._cadence_sec
                    )
                # Harvest each completed clean slot, computing its RTP window offset
                # from radiod's CURRENT mapping (anchor_utc_now) — not a frozen grid.
                while True:
                    start_off = round(
                        (self._next_boundary_utc - anchor_utc_now) * self._sr
                    )
                    if latest_off < start_off + cadence_samples + settle_samples:
                        break
                    # Everything the sidecar's timing block says comes from this one snapshot,
                    # taken with the clock locked: the anchor, its RTP, its re-pinned UTC and
                    # the slot's offset belong to one reference space.
                    snapshot = None
                    if self._get_anchor is not None:
                        snapshot = {
                            "anchor": self._get_anchor(),
                            "anchor_rtp": int(self._clock.rtp_of_offset(0)),
                            "label_anchor_utc": float(anchor_utc_now),
                            "first_sample_offset": int(start_off),
                            "batch_rtp_basis": (self._get_rtp_basis()
                                                if self._get_rtp_basis is not None else None),
                        }
                    harvested.append((start_off, self._next_boundary_utc, snapshot))
                    self._next_boundary_utc += self._cadence_sec
        except SlotClockDesyncError as exc:
            logger.error(
                "%s %d Hz: SlotClock desync in harvest — %s; requesting "
                "anchor reset (audit F18)",
                self._mode.upper(), self._frequency_hz, exc,
            )
            if self._on_desync is not None:
                self._on_desync()
            return

        for start_off, start_utc, snapshot in harvested:
            samples = self._ring.extract_by_offset(start_off, cadence_samples)
            if samples is None:
                self.slots_empty += 1
                self._episode(
                    "insufficient", 1,
                    f"slot at {start_utc:.1f} has insufficient samples, skipping; logged once "
                    "until the slots are complete again",
                    "slots complete again")
                continue
            self._episode("insufficient", 0, "", "slots complete again")
            wav_path = self._write_spool_wav(samples, start_utc, start_off, snapshot)
            self._fork_decoder(wav_path, start_utc)

    def _write_spool_wav(self, samples, slot_start_utc: float,
                         start_off: Optional[int] = None,
                         snapshot: Optional[dict] = None) -> Path:
        # MSK144 slot boundaries are integer seconds (:00/:15/:30/:45), so
        # the filename label is exact.  We name the WAV with a 4-digit-year
        # WSJT-X-style stamp (YYYYMMDD_HHMMSS_<freqkhz>.wav); jt9 tolerates
        # this form (verified 2026-06-12).  The slot's authoritative UTC is
        # carried separately from the RTP-anchored slot_start when the
        # decode is normalized into the log — we do NOT depend on jt9's own
        # filename-derived time column.  WAV content is always extracted at
        # the true slot_start_utc; only the FILENAME label is rounded.
        ceiled = int(math.ceil(slot_start_utc))
        slot_time = time.gmtime(ceiled)
        freq_khz = self._frequency_hz // 1000
        filename = time.strftime("%Y%m%d_%H%M%S", slot_time) + f"_{freq_khz}.wav"
        wav_path = self._spool_dir / filename

        bad = int(samples.size - np.count_nonzero(np.isfinite(samples)))
        self._episode(
            "nonfinite", bad,
            f"slot at {slot_start_utc:.0f} holds {bad} non-finite samples (NaN or inf) of "
            f"{samples.size}; the audio for jt9 has them as 0, the stats skip them"
            + (", and the kept I/Q keeps them as received" if self._capture == "iq" else "")
            + "; logged once until the slots are clean again",
            "non-finite samples cleared")
        if self._get_anchor is not None:
            self._slot_timing[wav_path] = self._timing_block(slot_start_utc, snapshot)

        audio = samples
        if self._capture == "iq":
            # The I/Q first: it is the record.  A failed write costs the keep, never the decode.
            iq_path = self._iq_path(wav_path)
            try:
                write_iq_wav(iq_path, samples, self._ring.sample_rate, self._frequency_hz)
            except OSError as e:
                self._episode("iq_write", 1,
                              f"could not write the I/Q for {wav_path.name} ({e}); decoding "
                              "continues and keeps nothing; logged once until writes succeed",
                              "I/Q writes resumed")
            else:
                self._episode("iq_write", 0, "", "I/Q writes resumed")
            audio = iq_to_usb_audio(samples)

        writer = write_wav_f32 if self._audio_f32 else write_wav
        try:
            stats = writer(
                path=wav_path,
                samples=audio,
                sample_rate=self._ring.sample_rate,
                frequency_hz=self._frequency_hz,
                scale_db=self._msqp.scale_db if self._msqp is not None else None,
            )
        except BaseException:
            # No decode follows, so nothing else would delete what this slot left in the spool.
            wav_path.unlink(missing_ok=True)
            self._slot_timing.pop(wav_path, None)
            if self._capture == "iq":
                self._iq_path(wav_path).unlink(missing_ok=True)
            raise
        if self._capture == "iq":
            # The count that matters is the I/Q's: a NaN in Q alone never reaches the audio.
            stats = dataclasses.replace(stats, nonfinite_samples=bad)
        self._slot_stats[wav_path] = stats
        if self._msqp is not None:
            try:
                self._msqp.on_slot(slot_start_utc, stats)
            except Exception as e:  # noqa: BLE001 — the noise watch must never cost a decode
                self._msqp_fault("noise watch", e)
            else:
                self._msqp_ok("noise watch")
        return wav_path

    def _episode(self, key: str, count: int, start_text: str, end_text: str) -> None:
        """Log a condition that can last many slots once when it starts and once when it clears.

        ``count`` > 0 means the slot has the condition (a WARNING on the first such slot, silence
        on the next); 0 means it does not (an INFO on the first clean slot after an episode,
        saying how many slots and samples it lasted).
        """
        who = f"{self._mode.upper()} {self._frequency_hz} Hz"
        st = self._episodes.get(key)
        if count:
            if st is None:
                self._episodes[key] = [1, count]
                logger.warning("%s: %s", who, start_text)
            else:
                st[0] += 1
                st[1] += count
        elif st is not None:
            del self._episodes[key]
            logger.info("%s: %s after %d slots (%d samples in all)", who, end_text, st[0], st[1])

    def _timing_block(self, slot_start_utc: float, snapshot: Optional[dict]) -> dict:
        """The sidecar's ``timing`` block for one slot, from the snapshot taken at harvest.

        The snapshot (anchor, anchor_rtp, label_anchor_utc, first_sample_offset) was read with the
        clock locked, so a re-anchor or restore after the harvest cannot mix reference spaces
        into the block.  Nothing here reads the clock or the stream again.  Without a snapshot
        (no anchor provider, as in a bare worker) the block holds the exact start and the sample
        rate, and the rest is null.
        """
        block = {
            "slot_start_utc_exact": float(slot_start_utc),
            "first_sample_rtp": None,
            "first_sample_offset": None,
            "anchor_rtp": None,
            "label_anchor_utc": None,
            "batch_rtp_basis": None,
            "sample_rate": int(self._ring.sample_rate),
            "anchor": None,
            "timing_authority_applied": None,
            "notes": TIMING_NOTES,
        }
        if snapshot is None:
            return block
        anchor_rtp, off = snapshot["anchor_rtp"], snapshot["first_sample_offset"]
        block["anchor_rtp"] = anchor_rtp
        block["first_sample_offset"] = off
        block["first_sample_rtp"] = (anchor_rtp + off) & 0xFFFFFFFF
        block["label_anchor_utc"] = snapshot["label_anchor_utc"]
        block["batch_rtp_basis"] = snapshot.get("batch_rtp_basis")
        anchor = snapshot["anchor"]
        if anchor is not None:
            block["anchor"] = {"utc": anchor.utc, "source": anchor.source,
                               "rtp_referenced": anchor.rtp_referenced,
                               "offset_ns": anchor.offset_ns}
            block["timing_authority_applied"] = anchor.timing_authority_applied(
                self._client_radiod)
        return block

    def _msqp_fault(self, what: str, e: Exception) -> None:
        self.msqp_errors += 1
        if not self._msqp_faulty.get(what):
            self._msqp_faulty[what] = True
            logger.warning("%s %d Hz: MSQP %s failed (%s: %s); decoding continues; "
                           "logged once until it recovers",
                           self._mode.upper(), self._frequency_hz, what,
                           e.__class__.__name__, e, exc_info=True)

    def _msqp_ok(self, what: str) -> None:
        if self._msqp_faulty.get(what):
            self._msqp_faulty[what] = False
            logger.info("%s %d Hz: MSQP %s resumed", self._mode.upper(), self._frequency_hz,
                        what)

    @staticmethod
    def _iq_path(wav_path: Path) -> Path:
        """The spool file holding this slot's I/Q: ``<stem>.iq.wav`` beside ``<stem>.wav``."""
        return wav_path.with_suffix(".iq.wav")

    def _finish_slot(self, wav_path: Path, slot_start: float, decodes: list) -> None:
        """Archive a decoded recording, or delete it.  Never leaves a spool file behind.

        The recording is the audio WAV, or in I/Q mode the I/Q WAV; the audio file goes either way.
        """
        stats = self._slot_stats.pop(wav_path, None)
        timing = self._slot_timing.pop(wav_path, None)
        recording = self._iq_path(wav_path) if self._capture == "iq" else wav_path
        # An I/Q file that never landed (failed write) leaves nothing to archive.
        if (self._msqp is not None and decodes and stats is not None
                and recording.exists()):
            extra = {"timing": timing} if timing is not None else {}
            try:
                kept = self._msqp.on_decoded(recording, slot_start, self._sr, stats, decodes,
                                             **extra)
            except Exception as e:  # noqa: BLE001 — archiving must never cost a decode
                self._msqp_fault("archive", e)
            else:
                self._msqp_ok("archive")
                if kept:
                    wav_path.unlink(missing_ok=True)   # moved into the archive; audio is no record
                    return
        wav_path.unlink(missing_ok=True)
        recording.unlink(missing_ok=True)

    def _fork_decoder(self, wav_path: Path, slot_start: float) -> None:
        self._fork_decoder_jt9(wav_path, slot_start)

    def _fork_decoder_jt9(self, wav_path: Path, slot_start: float) -> None:
        """WSJT-X jt9 in MSK144 mode — decodes arrive on the process STDOUT.

        CLI: ``jt9 -Y --msk144 -p <tr> -f 1500 -a <workdir> <wav>`` (run with
        cwd=<workdir>).  jt9 --msk144 prints each decode to stdout as
        ``<time> <snr> <dt> <freq> & <message>`` followed by a
        ``<DecodeFinished>`` sentinel; the ``-a`` workdir's ``decoded.txt``
        stays empty for MSK144.  We capture each slot's decodes from the
        proc's stdout at reap time (see _reap_finished/_materialise).
        """
        cmd = _decoder.build_jt9_cmd(
            self._decoder_path, self._workdir, wav_path,
            tr_period_sec=int(round(self._cadence_sec)),
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._workdir),
            )
            self._pending_procs.append(
                (proc, wav_path, slot_start, time.monotonic())
            )
            logger.debug(
                "%s %d Hz: jt9 --msk144 pid=%d on %s",
                self._mode.upper(), self._frequency_hz, proc.pid, wav_path.name,
            )
        except OSError as exc:
            logger.error("Failed to launch jt9: %s", exc)
            self._finish_slot(wav_path, slot_start, [])

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        """Kill a hung decoder and free its zombie + stdio FDs immediately."""
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)  # reap the zombie
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _drain_proc_pipes(self, proc: subprocess.Popen) -> None:
        """Read + discard a finished proc's stdout/stderr, then close them.

        stdout (the MSK144 decodes) is normally consumed first by
        _read_proc_stdout; this drains any remainder plus stderr and
        closes both FDs so they don't leak across slots.  Also used on
        the failure/timeout paths where stdout isn't harvested.
        """
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.read()
                    stream.close()
            except (OSError, ValueError):
                pass

    def _reap_finished(self) -> None:
        now = time.monotonic()
        still_pending = []
        for proc, wav_path, slot_start, fork_mono in self._pending_procs:
            ret = proc.poll()
            if ret is None:
                # Bound the leak: a proc still alive after DECODE_TIMEOUT_SEC
                # is hung.  Left here it leaks its two stdio FDs + the spool
                # WAV forever; left unbounded it grows until the MemoryMax
                # cgroup OOM-kills the daemon and Restart=always re-enters
                # the same state.  Kill, count a failure, drop it.
                if now - fork_mono > DECODE_TIMEOUT_SEC:
                    logger.warning(
                        "%s %d Hz: jt9 pid=%d on %s exceeded %.0fs "
                        "deadline — killing (hung decode)",
                        self._mode.upper(), self._frequency_hz, proc.pid,
                        wav_path.name, DECODE_TIMEOUT_SEC,
                    )
                    self.decodes_fail += 1
                    self._kill_proc(proc)
                    self._finish_slot(wav_path, slot_start, [])
                    continue
                still_pending.append(
                    (proc, wav_path, slot_start, fork_mono)
                )
                continue
            # jt9 exits 0 on a clean run whether or not it found anything.
            decodes: list = []
            if ret == 0:
                stdout_text = self._read_proc_stdout(proc)
                if _decoder.is_jt9_usage_text(stdout_text):
                    self._jt9_usage_fault()
                else:
                    self.decodes_ok += 1
                    self._jt9_usage_seen = False
                    decodes = self._materialise_jt9_output(
                        wav_path, slot_start, stdout_text)
                self._drain_proc_pipes(proc)
            else:
                self.decodes_fail += 1
                stderr = ""
                try:
                    if proc.stderr is not None:
                        stderr = proc.stderr.read().decode(
                            errors="replace").strip()[:200]
                except (OSError, ValueError):
                    pass
                self._drain_proc_pipes(proc)
                logger.warning(
                    "jt9 exit %d for %s: %s", ret, wav_path.name, stderr,
                )
            self._finish_slot(wav_path, slot_start, decodes)
        self._pending_procs = still_pending

    def _jt9_usage_fault(self) -> None:
        """jt9 printed its usage, so it rejected an option: count a failure, not a decode."""
        self.decodes_fail += 1
        if not self._jt9_usage_seen:
            self._jt9_usage_seen = True
            logger.error("%s %d Hz: jt9 %s printed its usage instead of decoding; it does "
                         "not take this command line (no MSK144?); logged once until "
                         "it decodes again", self._mode.upper(), self._frequency_hz,
                         self._decoder_path)

    def _reap_all(self, wait: bool = False) -> None:
        for proc, wav_path, slot_start, _fork_mono in self._pending_procs:
            decodes: list = []
            if wait:
                try:
                    proc.wait(timeout=5.0)
                    if proc.returncode == 0:
                        stdout_text = self._read_proc_stdout(proc)
                        if _decoder.is_jt9_usage_text(stdout_text):
                            self._jt9_usage_fault()
                        else:
                            decodes = self._materialise_jt9_output(
                                wav_path, slot_start, stdout_text)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._drain_proc_pipes(proc)
            self._finish_slot(wav_path, slot_start, decodes)
        self._pending_procs.clear()

    @staticmethod
    def _read_proc_stdout(proc: subprocess.Popen) -> str:
        """Read a finished jt9 proc's stdout (bytes → str). '' on any error.

        jt9 --msk144 emits its decodes here; the process has already
        exited (poll()/wait() returned) so the pipe holds the full,
        bounded output (a few decode lines + the <DecodeFinished> sentinel).
        """
        try:
            if proc.stdout is None:
                return ""
            return proc.stdout.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def _materialise_jt9_output(
        self, wav_path: Path, slot_start: float, stdout_text: str,
    ) -> list[dict]:
        """Parse this slot's jt9 stdout decodes, normalize, append to log.

        jt9 --msk144 prints each decode to stdout (NOT decoded.txt, which
        stays empty for MSK144).  Each decode line is normalized to a
        self-contained per-mode-log line (full UTC from the slot anchor,
        absolute RF frequency) that ChTailer parses.  When ``spool_spots``
        is set, the same lines are teed to a per-slot ``.spots.txt`` for
        the hs-uploader file-fallback path.  The ``<DecodeFinished>``
        sentinel and blank lines are dropped by the parser.

        Returns the parsed decodes (``parse_decoded_txt_line`` dicts, each with
        jt9's raw stdout line under ``jt9_line``), ``[]`` when none parsed: an
        empty list means the slot is not archived.
        """
        raw = [ln for ln in stdout_text.splitlines() if ln.strip()]
        if not raw:
            return []

        slot_struct = time.gmtime(int(math.ceil(slot_start)))
        out_lines: list[str] = []
        decodes: list[dict] = []
        for line in raw:
            decode = _decoder.parse_decoded_txt_line(line)
            if decode is None:
                continue
            out_lines.append(
                _decoder.normalize_log_line(
                    decode, slot_struct, self._frequency_hz,
                )
            )
            # jt9's own line rides beside the parsed fields into the MSQP sidecar.
            decodes.append({**decode, "jt9_line": line})
        if not out_lines:
            return []

        try:
            for ln in out_lines:
                self._log_fd.write(ln)
            self._log_fd.flush()
        except OSError as exc:
            logger.warning(
                "%s: failed appending jt9 output to log: %s",
                self._mode.upper(), exc,
            )

        if self._spool_spots:
            spots_path = wav_path.with_suffix(".spots.txt")
            try:
                spots_path.parent.mkdir(parents=True, exist_ok=True)
                with open(spots_path, "w", encoding="utf-8") as f:
                    f.writelines(out_lines)
            except OSError as exc:
                logger.warning(
                    "%s: failed writing per-slot spots file %s: %s",
                    self._mode.upper(), spots_path, exc,
                )
        return decodes
