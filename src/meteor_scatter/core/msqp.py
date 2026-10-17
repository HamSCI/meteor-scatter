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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from meteor_scatter.core.archive import Archive, band_label, instance_dirname
from meteor_scatter.core.frontend import FrontendLog
from meteor_scatter.core.noise_watch import NoiseWatch
from meteor_scatter.core.wav import WavStats

logger = logging.getLogger(__name__)

# What ReceiverManager asks radiod for on every channel (ensure_channel / add_channel).
CHANNEL_REQUESTED = {"agc_enable": 0, "gain_db": 0.0, "encoding": "f32", "preset": "usb"}


def build_sidecar(*, reporter_id, freq_hz, slot_start_utc, sample_rate, stats: WavStats,
                  scale_db, decodes, frontend: dict, version) -> dict:
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
        "channel_requested": dict(CHANNEL_REQUESTED),
        "decodes": decodes,
        "frontend": frontend,
        "software_version": version,
    }


class MsqpChannel:
    def __init__(self, archive: Archive, frontend: FrontendLog, freq_hz: int, reporter_id: str,
                 scale_db: Optional[float], noise: NoiseWatch, version: str,
                 cadence_sec: float = 30.0):
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

    def on_slot(self, slot_start_utc: float, stats: WavStats) -> None:
        fe = self._frontend.latest()
        msg = self._noise.observe(slot_start_utc, stats.noise_dbfs, fe.rf_gain if fe else None)
        if msg:
            logger.warning("%s: %s", band_label(self.freq_hz), msg)

    def on_decoded(self, wav_path: Path, slot_start_utc: float, sample_rate: int,
                   stats: WavStats, decodes: list) -> bool:
        """Keep one decoded recording.  True when it moved into the archive; False leaves
        wav_path for the caller to delete."""
        frontend = self._frontend.window(slot_start_utc, slot_start_utc + self._cadence,
                                         sample_rate)
        side = build_sidecar(reporter_id=self._reporter_id, freq_hz=self.freq_hz,
                             slot_start_utc=slot_start_utc, sample_rate=sample_rate, stats=stats,
                             scale_db=self.scale_db, decodes=decodes, frontend=frontend,
                             version=self._version)
        try:
            self._archive.keep(wav_path, self.freq_hz, slot_start_utc, side)
        except OSError as e:
            self.archive_failures += 1
            if not self._failing:
                self._failing = True
                logger.warning("%s: archiving stopped (%s); decoding and spots continue",
                               band_label(self.freq_hz), e)
            return False
        if self._failing:
            self._failing = False
            logger.info("%s: archiving resumed", band_label(self.freq_hz))
        self.archived += 1
        return True

    def state(self) -> dict:
        return {"band": band_label(self.freq_hz), "scale_db": self.scale_db,
                "archived": self.archived, "archive_failures": self.archive_failures,
                "archiving": not self._failing, "noise": self._noise.state()}


class MsqpRuntime:
    """One radiod's MSQP state: the archive, the front-end log and its listener, the bands.

    Each instance archives under its own root, <archive_dir>/<instance_dirname(instance_id)>,
    so two sources never write the same <band>/<day>/<stem>.wav."""

    def __init__(self, msqp_cfg: dict, version: str, instance_id: str):
        self.instance_id = instance_id
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
                cadence_sec=30.0) -> MsqpChannel:
        # A scoped source rebuild provisions the same bands again; keep their counters.
        for ch in self.channels:
            if ch.freq_hz == freq_hz:
                return ch
        ch = MsqpChannel(self.archive, self.frontend, freq_hz, reporter_id, scale_db,
                         NoiseWatch(noise_cal_dbfs), self._version, cadence_sec=cadence_sec)
        self.channels.append(ch)
        return ch

    def prune(self, today: date) -> list[str]:
        removed = self.archive.prune(today)
        cutoff = today - timedelta(days=self.archive.max_days)
        for label in removed:
            try:
                day = datetime.strptime(label.split("/")[1], "%Y%m%d").date()
                why = "age" if day < cutoff else "size"
            except (IndexError, ValueError):
                why = "?"
            logger.info("archive: removed %s (%s)", label, why)
        return removed

    def write_state(self, path: Path) -> None:
        fe = self.frontend.latest()
        now = time.time()
        state = {"written_at": now,
                 "archive": self.archive.usage(datetime.now(timezone.utc).date()),
                 "frontend_last_packet_age_sec": (now - fe.t) if fe else None,
                 "channels": [c.state() for c in self.channels]}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
