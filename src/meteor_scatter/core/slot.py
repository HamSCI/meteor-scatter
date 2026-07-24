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

MSK144 cadence: T/R slots at the configured tr_period_sec (default 30 s,
matching stock WSJT-X — slots at :00, :30; a 15 s config gives :00/:15/:30/:45).
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ka9q import SlotClock

from meteor_scatter.core import decoder as _decoder
from meteor_scatter.core.ring import Ring
from meteor_scatter.core.wav import write_wav

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
        keep_wav: bool = False,
        decoder_kind: str = DECODER_JT9,
        spool_spots: bool = False,
    ):
        if decoder_kind not in VALID_DECODER_KINDS:
            raise ValueError(
                f"decoder_kind must be one of {VALID_DECODER_KINDS}; "
                f"got {decoder_kind!r}"
            )
        self._ring = ring
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._cadence_sec = cadence_sec
        self._spool_dir = spool_dir
        self._log_fd = log_fd
        # Resolve the jt9 binary once (arch-specific bundled binary, or an
        # explicit override).  An empty decoder_path means "auto-resolve".
        self._decoder_path = _decoder.resolve_jt9_binary(decoder_path)
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
        # Next clean cadence-multiple UTC boundary to emit (None until first).
        self._next_boundary_utc: Optional[float] = None
        self._sr = clock.sample_rate
        self._decoder_kind = decoder_kind
        self._keep_wav = keep_wav
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
        self.slots_empty = 0

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
        anchor_utc_now = self._get_anchor_utc_now()
        if anchor_utc_now is None:
            return

        cadence_samples = self._clock.cadence_samples
        settle_samples = self._clock.settle_samples
        harvested: list[tuple[int, float]] = []
        with self._clock_lock:
            if not self._clock.anchored:
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
                harvested.append((start_off, self._next_boundary_utc))
                self._next_boundary_utc += self._cadence_sec

        for start_off, start_utc in harvested:
            samples = self._ring.extract_by_offset(start_off, cadence_samples)
            if samples is None:
                self.slots_empty += 1
                logger.warning(
                    "%s %d Hz: slot at %.1f — insufficient samples, skipping",
                    self._mode.upper(), self._frequency_hz, start_utc,
                )
                continue
            wav_path = self._write_spool_wav(samples, start_utc)
            self._fork_decoder(wav_path, start_utc)

    def _write_spool_wav(self, samples, slot_start_utc: float) -> Path:
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

        write_wav(
            path=wav_path,
            samples=samples,
            sample_rate=self._ring.sample_rate,
            frequency_hz=self._frequency_hz,
        )
        return wav_path

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
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)

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
                    if not self._keep_wav:
                        wav_path.unlink(missing_ok=True)
                    continue
                still_pending.append(
                    (proc, wav_path, slot_start, fork_mono)
                )
                continue
            # jt9 exits 0 on a clean run whether or not it found anything.
            if ret == 0:
                self.decodes_ok += 1
                stdout_text = self._read_proc_stdout(proc)
                self._materialise_jt9_output(wav_path, slot_start, stdout_text)
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
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs = still_pending

    def _reap_all(self, wait: bool = False) -> None:
        for proc, wav_path, slot_start, _fork_mono in self._pending_procs:
            if wait:
                try:
                    proc.wait(timeout=5.0)
                    if proc.returncode == 0:
                        stdout_text = self._read_proc_stdout(proc)
                        self._materialise_jt9_output(
                            wav_path, slot_start, stdout_text)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._drain_proc_pipes(proc)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
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
    ) -> None:
        """Parse this slot's jt9 stdout decodes, normalize, append to log.

        jt9 --msk144 prints each decode to stdout (NOT decoded.txt, which
        stays empty for MSK144).  Each decode line is normalized to a
        self-contained per-mode-log line (full UTC from the slot anchor,
        absolute RF frequency) that ChTailer parses.  When ``spool_spots``
        is set, the same lines are teed to a per-slot ``.spots.txt`` for
        the hs-uploader file-fallback path.  The ``<DecodeFinished>``
        sentinel and blank lines are dropped by the parser.
        """
        raw = [ln for ln in stdout_text.splitlines() if ln.strip()]
        if not raw:
            return

        slot_struct = time.gmtime(int(math.ceil(slot_start)))
        out_lines: list[str] = []
        for line in raw:
            decode = _decoder.parse_decoded_txt_line(line)
            if decode is None:
                continue
            out_lines.append(
                _decoder.normalize_log_line(
                    decode, slot_struct, self._frequency_hz,
                )
            )
        if not out_lines:
            return

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
