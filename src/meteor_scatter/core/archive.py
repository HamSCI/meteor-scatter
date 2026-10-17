"""Decoded MSK144 recordings kept for the Meteor Scatter QSO Party.

Layout: <root>/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav plus a .json sidecar.  A slot belongs to the
UTC day it starts on.  The spool and the archive share /var/lib/meteor-scatter, so keep() moves a
recording with a rename, with a copy fallback across filesystems.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BAND_NAMES = {28145000: "10m", 50260000: "6m"}


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def instance_dirname(name: str) -> str:
    """One instance's directory under msqp.archive_dir: the reporter id (or radiod id) made
    path-safe.  "/" and every character outside [A-Za-z0-9._-] become "-"; "AC0G/B4" -> "AC0G-B4".
    A name that would resolve to the archive_dir itself or its parent ("", ".", "..") becomes
    "unknown"."""
    safe = _UNSAFE.sub("-", str(name))
    return "unknown" if safe.strip(".") == "" else safe


def band_label(freq_hz: int) -> str:
    return BAND_NAMES.get(int(freq_hz), f"{int(freq_hz) // 1000}kHz")


def _slot_struct(slot_start_utc: float):
    # Same rounding as SlotWorker._write_spool_wav: slot boundaries fall on whole seconds.
    return time.gmtime(int(math.ceil(slot_start_utc)))


def slot_stem(slot_start_utc: float) -> str:
    return time.strftime("%y%m%d_%H%M%S", _slot_struct(slot_start_utc))


def slot_day(slot_start_utc: float) -> date:
    s = _slot_struct(slot_start_utc)
    return date(s.tm_year, s.tm_mon, s.tm_mday)


class Archive:
    def __init__(self, root: Path, max_days: int, max_gb: float):
        self.root = Path(root)
        self.max_days = int(max_days)
        self.max_bytes = int(float(max_gb) * 1e9)

    def keep(self, wav_path: Path, freq_hz: int, slot_start_utc: float, sidecar: dict) -> Path:
        """Move one decoded recording in and write its sidecar.  Raises OSError; restores the recording
        and removes only files this call created."""
        day_dir = self.root / band_label(freq_hz) / slot_day(slot_start_utc).strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / f"{slot_stem(slot_start_utc)}.wav"
        side = dest.with_suffix(".json")
        tmp = side.with_suffix(".json.part")
        wav_moved = False
        try:
            tmp.write_text(json.dumps(sidecar, indent=1, sort_keys=True))
            try:
                os.replace(wav_path, dest)
                wav_moved = True
            except OSError:
                shutil.move(str(wav_path), str(dest))
                wav_moved = True
            os.replace(tmp, side)
        except OSError:
            # Recovery: restore the wav if we moved it, and clean up only files we created.
            if wav_moved:
                try:
                    os.replace(dest, wav_path)
                except OSError:
                    # If os.replace fails, try shutil.move as fallback
                    try:
                        shutil.move(str(dest), str(wav_path))
                    except OSError:
                        pass  # If restore fails, leave dest as-is for manual recovery
            # Clean up tmp only if we created it
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return dest

    def _days(self):
        """[(day, 'band/YYYYMMDD', path)] sorted oldest first."""
        out = []
        if not self.root.is_dir():
            return out
        for band in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for d in band.iterdir():
                try:
                    day = datetime.strptime(d.name, "%Y%m%d").date()
                except ValueError:
                    continue
                if d.is_dir():
                    out.append((day, f"{band.name}/{d.name}", d))
        out.sort(key=lambda t: (t[0], t[1]))
        return out

    @staticmethod
    def _size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    def prune(self, today: date) -> list[str]:
        removed = []
        days = self._days()
        cutoff = today - timedelta(days=self.max_days)
        for day, label, path in list(days):
            if day < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(label)
                days.remove((day, label, path))
        sizes = {label: self._size(path) for _, label, path in days}
        total = sum(sizes.values())
        for day, label, path in days:
            if total <= self.max_bytes:
                break
            shutil.rmtree(path, ignore_errors=True)
            removed.append(label)
            total -= sizes[label]
        return removed

    def usage(self, today: date) -> dict:
        days = self._days()
        tag = today.strftime("%Y%m%d")
        return {
            "bytes": sum(self._size(p) for _, _, p in days),
            "oldest_day": days[0][1].split("/")[1] if days else None,
            "kept_today": sum(len(list(p.glob("*.wav"))) for _, l, p in days if l.endswith(tag)),
        }

    def recordings(self, first: date, last: date) -> list[tuple[str, Path, Path]]:
        out = []
        for day, label, path in self._days():
            if first <= day <= last:
                band = label.split("/")[0]
                for wav in sorted(path.glob("*.wav")):
                    out.append((band, wav, wav.with_suffix(".json")))
        return out
