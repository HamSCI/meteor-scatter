"""Spot-log tailer for msk144-recorder (CONTRACT v0.6 §17).

Watches the per-mode spot-log file `<log_dir>/<radiod_id>-msk144.log`,
parses each new line, and inserts rows into `msk144.spots` via
`sigmond.hamsci_sink.Writer.from_env()`.  Runs as a daemon thread inside
the Msk144Recorder process, feeding the cycle batcher → sink → wsprdaemon
upload path.

`Writer.from_env()` stages rows into sigmond's local SQLite sink by
default (`/var/lib/sigmond/sink.db`); `hs-uploader`'s reader is the other
half.  The writer resolves to a clean no-op only when the sink path is
unwritable (e.g. a standalone host outside a sigmond install).

Wire format (normalized by `slot.py` from jt9's `decoded.txt`, see
`core.decoder.normalize_log_line`):

    YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>

The `&` is the MSK144 sync indicator; `<abs_freq_hz>` is the true RF
frequency (channel dial + jt9 audio offset).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Line parser ─────────────────────────────────────────────────────────────

# Standard callsigns + WSJT-X compound forms:
#   * standard ITU call:                        K1ABC, AC0G, JA1AAA
#   * suffix-form compound:                     K1ABC/QRP, K1ABC/MM
#   * prefix-form compound (region/portable):   VE3/K1ABC, G/K1ABC, KH6/AC0G
# The regex is intentionally lossy — it's a best-effort filter for
# the freeform message field; the raw `message` text is always
# preserved by the caller.
_CALL_RE = re.compile(
    r"^"
    r"(?:[A-Z0-9]{1,3}/)?"               # optional prefix (e.g. "VE3/", "G/")
    r"[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}"    # standard call body (XX[X][D][YY[Y][Y]])
    r"(?:/[A-Z0-9]{1,4})?"                # optional suffix (e.g. "/QRP", "/MM")
    r"$"
)
# Maidenhead 6-char form has uppercase field+square but lowercase
# subsquare (per IARU convention).  Tolerate either case for robustness.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")
_REPORT_RE = re.compile(r"^R?([+-]?\d+)$")

# Format-detection regex for the decoder line router.
_DECODE_LINE_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2}\s")     # YYYY/MM/DD …
# MSK144 sync indicator slot.py writes between the numeric head and the
# freeform message (see core.decoder.normalize_log_line).
_MSK144_SEP = "&"


def parse_decoder_line(line: str, *, mode: Optional[str] = None) -> Optional[dict]:
    """Detect the normalized MSK144 log-line format and parse.

    The per-mode log file (`<radiod_id>-msk144.log`) carries lines that
    ``slot.py`` normalized from jt9's ``decoded.txt`` output (see
    ``core.decoder.normalize_log_line``).  We confirm the leading
    ``YYYY/MM/DD`` structure before parsing.

    Returns ``None`` on unrecognised structure (header line, blank,
    junk).  Caller should skip silently.
    """
    stripped = line.strip()
    if not stripped:
        return None
    if _DECODE_LINE_PREFIX.match(stripped):
        return parse_jt9_msk144_line(stripped, mode=mode or "msk144")
    return None


def parse_jt9_msk144_line(line: str, *, mode: str) -> Optional[dict]:
    """Parse one normalized MSK144 log line into a spot row.

    Expected shape (emitted by ``core.decoder.normalize_log_line``)::

        YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>

    The leading datetime is the RTP-anchored slot UTC; ``<abs_freq_hz>``
    is the true RF frequency (channel dial + jt9 audio offset); the
    value before ``&`` is jt9's SNR in dB (a real calibrated dB, unlike
    decode_ft8's internal "score").  Returns None on any parse failure —
    callers should skip silently.
    """
    line = line.strip()
    if not line or _MSK144_SEP not in line:
        return None
    head, _, message = line.partition(_MSK144_SEP)
    parts = head.split()
    if len(parts) < 5:
        return None
    try:
        # jt9 / the slot worker emit UTC; tag tz-aware so the sink writer
        # serializes unambiguously rather than guessing a local timezone.
        ts = datetime.strptime(
            parts[0] + " " + parts[1], "%Y/%m/%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        snr_db = int(float(parts[2]))
        dt = float(parts[3])
        freq = float(parts[4].replace(",", "").replace(" ", ""))
    except (ValueError, IndexError):
        return None

    message = message.strip()
    parsed = _parse_message(message)
    return {
        "time":               ts,
        "mode":               mode,
        "decoder_kind":       "jt9",
        "score":              None,        # jt9 reports calibrated dB, not a score
        "snr_db":             snr_db,
        "spectral_width_hz":  None,        # not surfaced for MSK144
        "dt":                 dt,
        "frequency":          int(freq),
        "frequency_mhz":      freq / 1_000_000.0,
        "message":            message,
        "tx_call":            parsed.get("tx_call", ""),
        "rx_call":            parsed.get("rx_call", ""),
        "grid":               parsed.get("grid", ""),
        "report":             parsed.get("report"),
    }


def _parse_message(message: str) -> dict:
    """Best-effort parse of a decoded MSK144 (WSJT-X) message body.

    Recognized shapes (all approximate; freeform messages return empty):
      "CQ <tx_call> [<grid>]"           — directed CQ
      "<rx_call> <tx_call> <grid>"      — first contact w/ grid
      "<rx_call> <tx_call> [R]<report>" — signal report
      "<rx_call> <tx_call> [73|RR73]"   — close

    Anything not matching shape returns whatever fields we can pull
    out, the rest empty.  The raw `message` text is always preserved
    by the caller.
    """
    out: dict[str, Any] = {}
    tokens = message.split()
    if not tokens:
        return out

    # Slice off the first 1–3 tokens for call positions.
    if tokens[0] == "CQ":
        # "CQ [target] <tx_call> [grid]" — `target` may be a region
        # tag like "DX", "EU", "POTA" that isn't a callsign.  Scan
        # past non-call tokens until we hit the first call-shaped one
        # (the sender), then look for a grid in the remaining tokens.
        # Bracketed compound calls (`<K1ABC/QRP>`) are stripped before
        # the regex match so they land as the tx_call.
        for i, tok in enumerate(tokens[1:], start=1):
            candidate = _strip_call_brackets(tok)
            if candidate is not None and _CALL_RE.match(candidate):
                out["tx_call"] = candidate
                for later in tokens[i + 1:]:
                    if _GRID_RE.match(later):
                        out["grid"] = later
                        break
                break
    else:
        # <rx_call> <tx_call> [grid|report|RR73|73]
        # Compound callsigns may appear bracketed (`<K1ABC/QRP>`) — that's
        # what a WSJT-X-protocol decoder substitutes when it resolved a
        # hash from its session table.  Strip the brackets before
        # matching so the call lands in the row instead of being dropped.
        rx_candidate = _strip_call_brackets(tokens[0])
        if rx_candidate is not None and _CALL_RE.match(rx_candidate):
            out["rx_call"] = rx_candidate
        if len(tokens) >= 2:
            tx_candidate = _strip_call_brackets(tokens[1])
            if tx_candidate is not None and _CALL_RE.match(tx_candidate):
                out["tx_call"] = tx_candidate
        if len(tokens) >= 3:
            tail = tokens[2]
            if _GRID_RE.match(tail):
                out["grid"] = tail
            else:
                m = _REPORT_RE.match(tail)
                if m:
                    try:
                        out["report"] = int(m.group(1))
                    except ValueError:
                        pass
    return out


def _strip_call_brackets(token: str) -> Optional[str]:
    """Strip surrounding ``<>`` from a token if it matches the WSJT-X
    bracketed-call shape.

    Returns the stripped call, or the original token if no brackets.
    Returns ``None`` for the literal "<...>" placeholder (unresolved
    hash, no recoverable callsign).
    """
    if not token:
        return token
    if token == "<...>":
        return None
    if token.startswith("<") and token.endswith(">") and len(token) > 2:
        return token[1:-1]
    return token


# ── Tailer ──────────────────────────────────────────────────────────────────

class ChTailer:
    """One tailer per (radiod, mode) log file.

    Spawns a daemon thread that polls the log for new lines, parses
    them, and inserts rows into `psk.spots` via hamsci_sink.Writer.
    Clean no-op only when the sink path is unwritable.
    """

    POLL_INTERVAL_SEC = 1.0       # how often to read new lines
    FLUSH_INTERVAL_SEC = 15.0     # max age of an unflushed batch
    CALLHASH_SAVE_INTERVAL_SEC = 300.0  # persist callhash table at most every 5 min

    def __init__(
        self,
        *,
        log_path: Path,
        mode: str,
        radiod_id: str,
        host_call: str = "",
        host_grid: str = "",
        processing_version: str = "",
        batch_rows: int = 200,
        writer_factory=None,
        callhash_path: Optional[Path] = None,
        forward_to_pskreporter: bool = True,
        rx_source: str = "",
        cycle_batcher: Optional[object] = None,
        reporter_id: Optional[str] = None,
    ) -> None:
        self._log_path = Path(log_path)
        self._mode = mode
        self._radiod_id = radiod_id
        # Phase-3 per-instance reporter ID
        # (sigmond's MULTI-INSTANCE-ARCHITECTURE.md §3).  Falls back
        # to radiod_id so legacy single-instance deployments still
        # get a meaningful value in the spot row's reporter_id
        # field during the deprecation window.
        self._reporter_id = reporter_id or radiod_id
        self._host_call = host_call
        self._host_grid = host_grid
        self._processing_version = processing_version
        self._batch_rows = batch_rows
        self._writer_factory = writer_factory or _default_writer_factory
        self._writer = None
        # Canonical multi-rx source identifier — ``radiod:<status_address>``
        # for radiod-backed sources.  Defaults to ``radiod:<radiod_id>`` so
        # single-rx deployments and tests get a sensible non-empty value
        # without the caller having to supply one.  Phase A plumbing for
        # the multi-source pipeline planned in msk144-recorder.
        self._rx_source = rx_source or f"radiod:{radiod_id}"
        # Optional :class:`Msk144CycleBatcher` reference (Phase C).  When
        # set, rows flow through the batcher (cycle-aligned commit, log
        # line in WSPR-parity format, foundation for cross-rx dedup in
        # Phase D) and the local writer is unused.  When None, this
        # tailer owns its own writer and inserts directly — legacy
        # Phase A/B behaviour, kept so single-tailer tests don't need
        # to spin up a batcher.
        self._cycle_batcher = cycle_batcher
        # Vestigial per-row flag from the PSKReporter delivery model
        # (always False in this deposit-only build — recorder.py passes
        # forward_flag=False).  Retained on the row for schema parity with
        # the wspr/psk siblings; the wsprdaemon upload path ignores it.
        self._forward_to_pskreporter = bool(forward_to_pskreporter)

        # WSJT-X compound-callsign hash table.  Per-radiod (shared
        # across modes — same compound calls show up on FT8 and FT4).
        # When callhash_path is provided, the table is persisted across
        # daemon restarts so the cumulative resolution grows over
        # time.  Lazy-imported so the tailer remains importable on
        # hosts that don't have sigmond installed.
        self._callhash_path = callhash_path
        self._callhash = self._make_callhash_table(callhash_path)
        self._last_callhash_save = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_pos = 0
        self._last_flush = 0.0

    # ----- lifecycle -----

    def start(self) -> None:
        """Build the writer and start the polling thread.

        Returns immediately. If the writer resolves to a no-op (sink
        path unwritable) we still start the thread, so health stays
        observable via `is_active`.  Failure to import the writer
        package is logged and the thread exits.
        """
        # Skip the local writer construction when a batcher will own
        # the SQLite path.  The batcher's writer thread builds its own
        # connection (matches sqlite3 thread-affinity).
        if self._cycle_batcher is None:
            try:
                self._writer = self._writer_factory(self._batch_rows)
            except Exception as e:
                logger.warning(
                    "ch_tailer disabled (%s): %s", self._mode, e,
                )
                return
            if self._writer.is_noop:
                logger.debug(
                    "ch_tailer %s: sink writer is a no-op "
                    "(sink path unwritable)", self._mode,
                )
        # Skip historical content — only tail from current end.
        if self._log_path.exists():
            try:
                self._last_pos = self._log_path.stat().st_size
            except OSError:
                self._last_pos = 0
        self._stop.clear()
        self._last_flush = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"ch-tail-{self._mode}-{self._radiod_id}",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        # Final callhash persistence so any observations since the
        # last periodic save aren't lost.
        if self._callhash is not None and self._callhash_path is not None:
            try:
                self._callhash.save()
            except Exception as exc:
                logger.warning("ch_tailer %s: final callhash save failed: %s",
                               self._mode, exc)

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def health(self) -> str:
        # Batcher-backed tailers don't own a writer; the batcher's
        # writer is what reports up health-status.  Surface "ok" here
        # so the tailer's own thread liveness is the only signal we
        # gate on at this layer.
        if self._cycle_batcher is not None:
            return "ok"
        if self._writer is None:
            return "noop"
        return self._writer.health

    # ----- polling loop -----

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.POLL_INTERVAL_SEC):
                self._poll_once()
        except Exception:
            logger.exception("ch_tailer %s: unhandled error in poll loop", self._mode)

    def _poll_once(self) -> None:
        # Only the legacy direct-write path needs a writer; the
        # batcher path has its own writer thread upstream.
        if self._writer is None and self._cycle_batcher is None:
            return
        try:
            stat = self._log_path.stat()
        except FileNotFoundError:
            return
        size = stat.st_size
        if size < self._last_pos:
            # File was rotated; reset to head.
            self._last_pos = 0
        if size > self._last_pos:
            try:
                with open(self._log_path, "rb") as fh:
                    fh.seek(self._last_pos)
                    chunk = fh.read(size - self._last_pos)
                self._last_pos = size
            except OSError as e:
                logger.warning("ch_tailer %s: read failed: %s", self._mode, e)
                return
            self._consume(chunk.decode(errors="replace"))

        # Periodic flush even if no new data, so a partial batch
        # doesn't sit indefinitely.  Only applies to the legacy
        # direct-write path — the batcher's writer thread runs its
        # own deadline-based flushes.
        if (
            self._writer is not None
            and (time.monotonic() - self._last_flush)
                > self.FLUSH_INTERVAL_SEC
        ):
            try:
                self._writer.flush()
            except Exception as e:
                logger.warning("ch_tailer %s: flush failed: %s", self._mode, e)
            self._last_flush = time.monotonic()

    def _consume(self, text: str) -> None:
        # Feed the whole chunk to the callhash table first so any
        # `<call>` announcements (compound or resolved) are captured
        # in our cumulative cache before per-line parsing.  This is
        # cheap (a single regex scan) and makes the table grow without
        # caring about line boundaries.
        if self._callhash is not None:
            try:
                self._callhash.observe(text)
            except Exception as exc:
                logger.warning("ch_tailer %s: callhash observe failed: %s",
                               self._mode, exc)

        # Canonical timing-provenance block (CLIENT-CONTRACT §18), read
        # once per chunk — all decodes in a chunk share the slot's
        # authority state. Sourced from hf-timestd's adjudicated
        # authority.json; degrades to the standalone-fallback marker when
        # hf-timestd is absent/stale. Identical shape across all clients.
        from msk144_recorder.core.authority_reader import (
            AuthorityReader, standalone_timing_authority,
        )
        try:
            _snap = AuthorityReader().read()
        except Exception as exc:
            logger.debug("authority.json read failed: %s", exc)
            _snap = None
        timing_authority = (
            _snap.to_timing_authority(self._radiod_id)
            if _snap is not None
            else standalone_timing_authority(self._radiod_id)
        )

        rows: list[dict] = []
        for line in text.splitlines():
            row = parse_decoder_line(line, mode=self._mode)
            if row is None:
                continue
            row["timing_authority"] = timing_authority
            row["host_call"] = self._host_call
            row["host_grid"] = self._host_grid
            row["radiod_id"] = self._radiod_id
            row["instance"] = self._radiod_id   # legacy field — removed in Phase 9
            row["reporter_id"] = self._reporter_id
            row["rx_source"] = self._rx_source
            # Phase D Cut 2: 100 Hz bucket of the absolute decode
            # frequency.  PSKReporter's own dedup tolerance, and large
            # enough to collapse the ~1-5 Hz inter-receiver jitter we
            # see when the same TX is decoded by multiple radiod
            # instances (different host PPS / clock disciplines).
            # ``SqliteSource.dedup_partition_by`` keys on this so
            # cross-rx duplicates pick a single winner per
            # (time, tx_call, freq_bucket) before reaching
            # PskReporterTcp.  Missing/invalid frequency falls to 0
            # so the dedup partition treats it as a single group
            # (any malformed rows lose to a valid duplicate).
            try:
                row["frequency_bucket_hz"] = (
                    int(row.get("frequency") or 0) // 100 * 100
                )
            except (TypeError, ValueError):
                row["frequency_bucket_hz"] = 0
            row["processing_version"] = self._processing_version
            row["forward_to_pskreporter"] = self._forward_to_pskreporter
            rows.append(row)
        if rows:
            if self._cycle_batcher is not None:
                # Phase C: dispatch to the shared batcher.  It handles
                # cycle bucketing, the SQLite write, and the
                # cycle-commit log line.  The batcher's own writer
                # thread owns the SQLite connection.
                try:
                    self._cycle_batcher.add(
                        rows,
                        rx_source=self._rx_source,
                        radiod_id=self._radiod_id,
                    )
                except Exception as e:
                    logger.warning(
                        "ch_tailer %s: batcher add failed (%d rows): %s",
                        self._mode, len(rows), e,
                    )
            else:
                # Legacy direct-write path — kept for single-tailer
                # tests + any deployment that hasn't been migrated to
                # the batcher yet.
                try:
                    self._writer.insert(rows)
                except Exception as e:
                    logger.warning(
                        "ch_tailer %s: insert failed (%d rows): %s",
                        self._mode, len(rows), e,
                    )

        # Periodic callhash persistence.  CALLHASH_SAVE_INTERVAL_SEC is
        # generous (5 min) to amortise the JSON write across many
        # observations.  No-op when nothing changed since the last save.
        if (
            self._callhash is not None
            and self._callhash_path is not None
            and (time.monotonic() - self._last_callhash_save)
                > self.CALLHASH_SAVE_INTERVAL_SEC
        ):
            try:
                self._callhash.save()
            except Exception as exc:
                logger.warning("ch_tailer %s: callhash save failed: %s",
                               self._mode, exc)
            self._last_callhash_save = time.monotonic()

    def _make_callhash_table(self, path: Optional[Path]):
        """Construct (or load) the per-radiod CallHashTable.

        Returns None when ``callhash`` isn't importable — keeps
        msk144-recorder runnable on hosts without the callhash library.
        """
        try:
            from callhash import CallHashTable  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.debug(
                "ch_tailer %s: callhash library unavailable (%s); "
                "compound-callsign hash resolution disabled",
                self._mode, exc,
            )
            return None
        if path is None:
            return CallHashTable()
        return CallHashTable.load_or_new(path)


def _default_writer_factory(batch_rows: int):
    """Lazy-import `sigmond.hamsci_sink.Writer` for `msk144.spots`.

    Sigmond core stays stdlib-only; this import only happens when a
    tailer actually starts.  `Writer.from_env()` resolves the backend
    (sigmond's SQLite sink by default); the writer is itself a no-op
    when the sink path is unwritable.

    ``mode="msk144"`` namespaces rows into the ``msk144.spots`` target
    (the hamsci_sink mode is a free-form namespace — wspr/psk/noise
    coexist the same way).  ``schema_version=2`` is the tag every staged
    row carries; the `hs-uploader` reader filters on it, so the producer
    must tag rows at the matching version or the source silently treats
    them as stale-schema and yields nothing.
    """
    from sigmond.hamsci_sink import Writer
    return Writer.from_env(
        table="spots", mode="msk144",
        schema_version=2, batch_rows=batch_rows,
    )
