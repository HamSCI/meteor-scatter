"""Per-channel MSQP glue: build the sidecar, keep the recording, watch the noise floor.

MsqpRuntime holds one radiod's archive, front-end log and listener; MsqpChannel holds one band's
share of it.  SlotWorker calls on_slot() for every slot it writes and on_decoded() for every slot
jt9 decoded something in.  A recording that on_decoded() does not keep is the SlotWorker's to
delete, as before.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from meteor_scatter.core.archive import Archive, band_label, instance_dirname
from meteor_scatter.core.frontend import FrontendLog
from meteor_scatter.core.noise_watch import NoiseWatch
from meteor_scatter.core.wav import WavStats

logger = logging.getLogger(__name__)

NOISE_LOG_LINES = 120            # an hour of 30 s slots, for `msqp calibrate`


def channel_requested(preset: str) -> dict:
    """What ReceiverManager asks radiod for on every channel (ensure_channel / add_channel):
    AGC off at 0 dB, f32 (forced whatever the config says), and the configured preset."""
    return {"agc_enable": 0, "gain_db": 0.0, "encoding": "f32", "preset": preset}


def build_sidecar(*, reporter_id, freq_hz, slot_start_utc, sample_rate, stats: WavStats,
                  scale_db, decodes, frontend: dict, version, preset: str = "usb",
                  reported: Optional[dict] = None) -> dict:
    """One recording's sidecar.  noise_floor_dbfs is dB re radiod's f32 full scale (1.0),
    measured before wav_gain_db; the floor in the WAV is noise_floor_dbfs + wav_gain_db.
    channel_reported holds radiod's latest status report for the channel's frequency, or None
    when none has arrived."""
    return {
        "reporter_id": reporter_id,
        "band": band_label(freq_hz),
        "dial_freq_hz": int(freq_hz),
        "slot_start_utc": datetime.fromtimestamp(slot_start_utc, timezone.utc).isoformat(),
        "sample_rate": int(sample_rate),
        "scale": "uncalibrated" if scale_db is None else "calibrated",
        "wav_gain_db": stats.gain_db,
        "clipped_samples": stats.clipped_samples,
        "noise_floor_dbfs": stats.noise_dbfs,
        "noise_floor_reference": "dB re radiod f32 full scale, before wav_gain_db",
        "channel_requested": channel_requested(preset),
        "channel_reported": reported,
        "decodes": decodes,
        "frontend": frontend,
        "software_version": version,
    }


def _noise_line_ok(line: str) -> bool:
    """True for a well-formed "<slot_utc> <noise_dbfs>" noise-log line."""
    parts = line.split()
    if len(parts) != 2:
        return False
    try:
        int(parts[0]); float(parts[1])
    except ValueError:
        return False
    return True


class MsqpChannel:
    def __init__(self, archive: Archive, frontend: FrontendLog, freq_hz: int, reporter_id: str,
                 scale_db: Optional[float], noise: NoiseWatch, version: str,
                 cadence_sec: float = 30.0, noise_log: Optional[Path] = None,
                 preset: str = "usb"):
        self._archive = archive
        self._frontend = frontend
        self.freq_hz = freq_hz
        self._reporter_id = reporter_id
        self.scale_db = scale_db
        self._noise = noise
        self._version = version
        self._cadence = float(cadence_sec)
        self.archived = 0
        self.archive_failures = 0
        self._failing = False
        self.preset = preset
        self._noise_log = Path(noise_log) if noise_log else None
        self._noise_log_failing = False
        self.noise_log_failures = 0

    def on_slot(self, slot_start_utc: float, stats: WavStats) -> None:
        fe = self._frontend.latest()
        msg = self._noise.observe(slot_start_utc, stats.noise_dbfs, fe.rf_gain if fe else None)
        if msg:
            logger.warning("%s: %s", band_label(self.freq_hz), msg)
        self._log_noise(slot_start_utc, stats.noise_dbfs)

    def _log_noise(self, slot_start_utc: float, noise_dbfs: Optional[float]) -> None:
        """Append "<slot_utc> <noise_dbfs>" for `msqp calibrate`; keep the last
        NOISE_LOG_LINES.  A damaged line drops out on the next rewrite.  Best effort: any
        failure costs the log, never the decode, and logs once on the change and once on
        recovery."""
        if self._noise_log is None or noise_dbfs is None:
            return
        try:
            try:
                text = self._noise_log.read_text(errors="replace")
            except FileNotFoundError:
                text = ""
            lines = [ln for ln in text.splitlines() if _noise_line_ok(ln)]
            lines.append(f"{int(slot_start_utc)} {noise_dbfs:.1f}")
            self._noise_log.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._noise_log.with_suffix(".txt.part")
            tmp.write_text("\n".join(lines[-NOISE_LOG_LINES:]) + "\n")
            os.replace(tmp, self._noise_log)
        except Exception as e:  # noqa: BLE001 — a persistent fault logs on transition only
            self.noise_log_failures += 1
            if not self._noise_log_failing:
                self._noise_log_failing = True
                logger.warning("%s: noise log stopped (%s); decoding continues",
                               band_label(self.freq_hz), e)
            return
        if self._noise_log_failing:
            self._noise_log_failing = False
            logger.info("%s: noise log resumed", band_label(self.freq_hz))

    def on_decoded(self, wav_path: Path, slot_start_utc: float, sample_rate: int,
                   stats: WavStats, decodes: list) -> bool:
        """Keep one decoded recording.  True when it moved into the archive; False leaves
        wav_path for the caller to delete.  Any failure counts, warns once on the change and
        logs once on recovery: a persistent fault never writes a line per slot."""
        try:
            frontend = self._frontend.window(slot_start_utc, slot_start_utc + self._cadence,
                                             sample_rate)
            side = build_sidecar(reporter_id=self._reporter_id, freq_hz=self.freq_hz,
                                 slot_start_utc=slot_start_utc, sample_rate=sample_rate,
                                 stats=stats, scale_db=self.scale_db, decodes=decodes,
                                 frontend=frontend, version=self._version, preset=self.preset,
                                 reported=self._frontend.channel_reported(self.freq_hz))
            self._archive.keep(wav_path, self.freq_hz, slot_start_utc, side)
        except Exception as e:  # noqa: BLE001 — archiving must never cost a decode
            self.archive_failures += 1
            if not self._failing:
                self._failing = True
                what = e if isinstance(e, OSError) else f"{e.__class__.__name__}: {e}"
                logger.warning("%s: archiving stopped (%s); decoding and spots continue",
                               band_label(self.freq_hz), what)
            return False
        if self._failing:
            self._failing = False
            logger.info("%s: archiving resumed", band_label(self.freq_hz))
        self.archived += 1
        return True

    def state(self) -> dict:
        return {"band": band_label(self.freq_hz), "scale_db": self.scale_db,
                "archived": self.archived, "archive_failures": self.archive_failures,
                "archiving": not self._failing,
                "noise_log_failures": self.noise_log_failures,
                "noise_log": not self._noise_log_failing, "noise": self._noise.state()}


class MsqpRuntime:
    """One radiod's MSQP state: the archive, the front-end log and its listener, the bands.

    Each instance archives under its own root, <archive_dir>/<instance_dirname(instance_id)>,
    so two sources never write the same <band>/<day>/<stem>.wav."""

    def __init__(self, msqp_cfg: dict, version: str, instance_id: str,
                 spool_root: Optional[Path] = None):
        self.instance_id = instance_id
        self._spool_root = Path(spool_root) if spool_root else None
        self.archive = Archive(Path(msqp_cfg["archive_dir"]) / instance_dirname(instance_id),
                               msqp_cfg["archive_max_days"], msqp_cfg["archive_max_gb"])
        self.frontend = FrontendLog()
        self._listener = None
        self._version = version
        self.channels: list[MsqpChannel] = []

    def start_listener(self, status_address: str) -> None:
        """Best-effort: without the listener, sidecars report the front end as "missing"."""
        from meteor_scatter.core.frontend import FrontendListener
        try:
            if self._listener is None:
                self._listener = FrontendListener(status_address, self.frontend)
            self._listener.start()           # no-op while already running
        except Exception as e:  # noqa: BLE001
            logger.warning("front-end listener unavailable: %s", e)
            self._listener = None

    def stop_listener(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("front-end listener stop failed: %s", e)
        self._listener = None

    def channel(self, freq_hz, *, reporter_id, scale_db, noise_cal_dbfs,
                cadence_sec=30.0, preset="usb") -> MsqpChannel:
        # A scoped source rebuild provisions the same bands again; keep their counters.
        for ch in self.channels:
            if ch.freq_hz == freq_hz:
                return ch
        ch = MsqpChannel(self.archive, self.frontend, freq_hz, reporter_id, scale_db,
                         NoiseWatch(noise_cal_dbfs), self._version, cadence_sec=cadence_sec,
                         noise_log=(self._spool_root / f"noise-{int(freq_hz)}.txt"
                                    if self._spool_root else None),
                         preset=preset)
        self.channels.append(ch)
        return ch

    def prune(self, today: date) -> list[str]:
        removed = self.archive.prune(today)
        cutoff = self.archive.age_cutoff(today)
        for label in removed:
            try:
                day = datetime.strptime(label.split("/")[1], "%Y%m%d").date()
                why = "age" if day <= cutoff else "size"
            except (IndexError, ValueError):
                why = "?"
            logger.info("archive: removed %s (%s)", label, why)
        return removed

    def write_state(self, path: Path) -> None:
        fe = self.frontend.latest()
        now = time.time()
        state = {"written_at": now,
                 "archive_root": str(self.archive.root),
                 "archive": self.archive.usage(datetime.now(timezone.utc).date()),
                 "frontend_last_packet_age_sec": (now - fe.t) if fe else None,
                 "channels": [c.state() for c in self.channels]}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
