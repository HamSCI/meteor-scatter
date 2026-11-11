"""Decoded MSK144 recordings kept for the Meteor Scatter QSO Party.

Layout: <root>/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav plus a .json sidecar.  With
archive_dtype = "f16" a slot's I/Q is <stem>.iq16.zst instead (zstd of interleaved binary16 x 4096;
the sidecar says "dtype": "complex-f16").  A slot belongs to the UTC day it starts on.  The spool and the archive share /var/lib/meteor-scatter, so keep() moves a
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
from typing import Tuple

import numpy as np
import zstandard

from meteor_scatter.core import iq_codec
from meteor_scatter.core.iq import read_iq_bytes

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


F16_SUFFIX = ".iq16.zst"
ARCHIVE_DTYPES = ("complex64", "f16")


def _is_slot(p: Path) -> bool:
    """A kept slot's recording: <stem>.wav or <stem>.iq16.zst."""
    return p.name.endswith(".wav") or p.name.endswith(F16_SUFFIX)


def _sidecar_path(slot: Path) -> Path:
    name = slot.name
    stem = name[: -len(F16_SUFFIX)] if name.endswith(F16_SUFFIX) else slot.stem
    return slot.with_name(stem + ".json")


def read_slot_iq(path: Path) -> Tuple[np.ndarray, int]:
    """(complex64 I/Q, sample rate) of a kept slot, whether <stem>.wav or <stem>.iq16.zst.
    ValueError when the file holds no I/Q (an audio WAV) or the sidecar lacks what an F16 slot
    needs."""
    path = Path(path)
    if not path.name.endswith(F16_SUFFIX):
        return read_iq_bytes(path.read_bytes())
    try:
        side = json.loads(_sidecar_path(path).read_text())
        dtype, rate = side["dtype"], int(side["sample_rate"])
        scale = float(side.get("scale", 1.0))
    except (OSError, ValueError, KeyError) as e:
        raise ValueError(f"{path.name}: unreadable sidecar ({e})") from e
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    return iq_codec.decode_iq(raw, dtype, scale), rate


def slot_day(slot_start_utc: float) -> date:
    s = _slot_struct(slot_start_utc)
    return date(s.tm_year, s.tm_mon, s.tm_mday)


class Archive:
    def __init__(self, root: Path, max_days: int, max_gb: float,
                 archive_dtype: str = "complex64"):
        if archive_dtype not in ARCHIVE_DTYPES:
            raise ValueError(f"archive_dtype must be one of {ARCHIVE_DTYPES}; got {archive_dtype!r}")
        self.archive_dtype = archive_dtype
        self.root = Path(root)
        self.max_days = int(max_days)
        self.max_bytes = int(float(max_gb) * 1e9)

    def keep(self, wav_path: Path, freq_hz: int, slot_start_utc: float, sidecar: dict) -> Path:
        """Move one decoded recording in and write its sidecar.  Raises what failed (OSError, or
        TypeError for a sidecar that will not serialise); restores the recording and removes only
        files this call created.

        With archive_dtype "f16" and an F32 I/Q WAV, the slot goes in as <stem>.iq16.zst and the
        WAV goes.  A WAV that holds audio, or I/Q that would overflow binary16, goes in as today;
        the overflow case says so in the sidecar ("f16_fallback")."""
        day_dir = self.root / band_label(freq_hz) / slot_day(slot_start_utc).strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / f"{slot_stem(slot_start_utc)}.wav"
        side = dest.with_suffix(".json")
        if self.archive_dtype == "f16":
            try:
                iq, rate = read_iq_bytes(Path(wav_path).read_bytes())
            except ValueError:
                iq = None                       # not I/Q: nothing to convert
            if iq is not None:
                payload = iq_codec.encode_f16(iq)
                if payload is None:
                    sidecar = {**sidecar, "f16_fallback": "overflow"}
                else:
                    return self._keep_f16(wav_path, payload, rate, day_dir, side, sidecar)
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
        except Exception:
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

    def _keep_f16(self, wav_path: Path, payload: bytes, rate: int, day_dir: Path, side: Path,
                  sidecar: dict) -> Path:
        dest = day_dir / (side.stem + F16_SUFFIX)
        part = dest.with_name(dest.name + ".part")
        tmp = side.with_suffix(".json.part")
        moved = False
        try:
            part.write_bytes(zstandard.ZstdCompressor().compress(payload))
            tmp.write_text(json.dumps({**sidecar, "dtype": iq_codec.DTYPE_F16,
                                       "scale": iq_codec.F16_SCALE, "sample_rate": int(rate)},
                                      indent=1, sort_keys=True))
            os.replace(part, dest)
            moved = True
            os.replace(tmp, side)
        except Exception:
            # The source WAV is untouched until here: remove only what this call created.
            for p in (part, tmp) + ((dest,) if moved else ()):
                try:
                    p.unlink()
                except OSError:
                    pass
            raise
        try:
            Path(wav_path).unlink()
        except OSError:
            pass                                # the spool pruner takes a leftover
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

    def age_cutoff(self, today: date) -> date:
        """The newest day the age cap deletes.  max_days counts today, so with the default 90
        the archive keeps today and the 89 days before it."""
        return today - timedelta(days=self.max_days)

    def prune(self, today: date) -> list[str]:
        removed = []
        days = self._days()
        cutoff = self.age_cutoff(today)
        for day, label, path in list(days):
            if day <= cutoff:
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
        """Size, oldest day and recordings kept today: in total, and per band under "bands"."""
        tag = today.strftime("%Y%m%d")
        bands: dict[str, dict] = {}
        for _, label, path in self._days():             # oldest first
            band, day = label.split("/")
            b = bands.setdefault(band, {"bytes": 0, "oldest_day": day, "kept_today": 0})
            b["bytes"] += self._size(path)
            if day == tag:
                b["kept_today"] += sum(1 for f in path.iterdir() if _is_slot(f))
        days = [b["oldest_day"] for b in bands.values()]
        return {
            "bytes": sum(b["bytes"] for b in bands.values()),
            "oldest_day": min(days) if days else None,
            "kept_today": sum(b["kept_today"] for b in bands.values()),
            "bands": bands,
        }

    def recordings(self, first: date, last: date) -> list[tuple[str, Path, Path]]:
        out = []
        for day, label, path in self._days():
            if first <= day <= last:
                band = label.split("/")[0]
                for slot in sorted(f for f in path.iterdir() if _is_slot(f)):
                    out.append((band, slot, _sidecar_path(slot)))
        return out
