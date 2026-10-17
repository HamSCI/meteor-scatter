# MSQP capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** meteor-scatter keeps every decoded MSK144 recording with a sidecar of its gains, packages a
shower's recordings the way the Meteor Scatter QSO Party asks, and carries them to an unsubmitted
Zenodo draft.

**Architecture:** The daemon gains four small units: a fixed-scale WAV writer that reports what it
did, an archive with a cap, a front-end gain log fed by a passive status listener, and a noise-floor
watch.  SlotWorker calls them after each decode.  Two operator commands, `msqp package` and
`msqp draft`, read the archive and talk to Zenodo; a third, `msqp calibrate`, sets each band's scale
from the noise the daemon already measures.

**Tech Stack:** Python 3.11+, numpy, ka9q-python ≥3.26 (`ka9q.status.decode_status_packet`), stdlib
`urllib.request` / `zipfile` / `json`, unittest + pytest via `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-10-17-msqp-capture-design.md` (approved 2026-10-17).  Read it
before any task.

## What the code showed that the spec did not know

Task 0 amends the spec with these, so spec and plan agree:

1. The channel already streams 32-bit float.  `receiver_manager.provision_channels` forces `f32`.
2. ka9q-python's `create_channel` already sends `AGC_ENABLE=0` and `GAIN=0.0` on every channel it
   creates.  Task 6 passes them explicitly so the request stays visible; Task 0 confirms on B4 that
   radiod reports AGC off on the live meteor channels.
3. The gain change between recordings comes from our own `core/wav.py`, which peak-normalizes each
   slot to −1 dBFS.  A calibrated band replaces that with its fixed `scale_db`.  An uncalibrated
   band keeps peak-normalization, so decoding never suffers, and the sidecar records the gain the
   writer applied to each recording, so every recording stays reducible to radiod's output level.
4. The front-end gains (`rf_gain`, `rf_atten`, `rf_agc`, `if_power`, `ad_over`) arrive in every
   status packet, but ka9q's `StatusListener` passes only timing fields to its callbacks.
   meteor-scatter therefore runs its own passive listener that decodes the full packet with
   `ka9q.status.decode_status_packet`.
5. `meteor-scatter status` prints "not implemented"; sigmond reads `inventory --json`.  The plan
   reports MSQP state in `inventory --json`.  Rendering it in `smd status` belongs to sigmond and
   goes on the v3.73 list as its own row (Task 13).
6. The site sink switch lives in `/etc/sigmond/coordination.toml` `[uploads]` (`mode` = `upload` |
   `hold` | `discard`; absent means `upload`).  No per-client sink state exists in code yet
   (`tasks/plan-sink-control.md` plans it).  `msqp draft` reads the site switch through one function,
   `site_upload_mode()`, which the per-client state can replace when sigmond ships it.

## Global Constraints

- Keep only recordings in which jt9 decoded at least one line; delete the rest as today.
- Archive layout: `/var/lib/meteor-scatter/archive/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav` + `.json`.
- Cap: `archive_max_days = 90`, `archive_max_gb = 2`, pruned hourly, each deletion logged.
- Calibration target: noise 50 dB below 16-bit full scale.  One `scale_db` per band.
- Noise watch: warn when a band's floor stays more than 10 dB from its calibrated level for one hour;
  one journal line on the change, one on recovery; never change the scale automatically.
- Front-end listener sends nothing to radiod.  A gap over 5 s marks `"frontend_status": "missing"`.
- Title form: `<CALL> <GRID6> <YYYYMMDD-DD> Meteor Scatter QSO Party`.
- Transmitter power reads `NA`; operating mode reads `Receive Only`.
- `msqp draft` never submits for review and never publishes; resumes one draft per package.
- `msqp draft` refuses unless the site upload mode reads `upload`, or when the token file is missing
  or readable by group or other.
- No test touches production Zenodo.  No new runtime dependency (HTTP through `urllib.request`).
- Run tests with `cd /home/mjh/hamsci/repos/meteor-scatter && uv run pytest tests/<file> -v`.
  Baseline before Task 1: 297 passed, 1 skipped.
- Commit messages end with the two attribution lines this session uses:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01H4HYgCZ3dRtK4XWA1Xyoc3`.

## Review Focus

1. A slot that starts at 23:59:30 UTC belongs to the day it starts on, in both the archive path and
   the package's date range (test in Task 2 and Task 9).
2. jt9 output that holds only `<DecodeFinished>` or unparseable lines counts as no decode: nothing
   archived (test in Task 6).
3. A full disk while archiving deletes the recording, counts it, warns once, and decoding continues;
   the next successful write logs one recovery line (test in Task 6).
4. `package` with `--from` after `--to`, or a range holding no recordings, refuses with a clear
   message and writes nothing (test in Task 10).
5. A `draft` that failed after creating the draft resumes the same draft on re-run and uploads only
   the files not yet committed (test in Task 12).

---

### Task 0: Settle the open facts and amend the spec

No product code.  Output: the answers written into the spec, and one constant for Task 11.

**Files:**
- Modify: `docs/superpowers/specs/2026-10-17-msqp-capture-design.md` §3.1, §3.2, §4, §5.3, §6
- Create (scratch, not committed): `/tmp/claude-1000/-home-mjh-hamsci/256365e9-8eb5-4061-aff1-c2dab3611aca/scratchpad/zenodo_probe.py`

- [ ] **Step 1: Confirm AGC off on B4's live meteor channels (read-only)**

Read the claude-bus first (CLAUDE.md).  Then on B4's VM, as the station user, listen to radiod's
status packets for five seconds.  The probe sends nothing and changes nothing.  Save it in the
scratchpad as `agc_probe.py`, copy it to the VM's `/tmp`, and run it with the meteor-scatter venv's
Python, passing the radiod status name from `/etc/meteor-scatter/*.toml` (`status = "..."`):

```python
# agc_probe.py <radiod-status-name> — passive; joins radiod's status group, prints the meteor
# channels' AGC and gain, and the front end's gain.
import socket, struct, sys, time
from ka9q.status import decode_status_packet
from ka9q.status_listener import DEFAULT_STATUS_PORT
from ka9q.utils import resolve_multicast_address

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
s.bind(("0.0.0.0", DEFAULT_STATUS_PORT))
grp = resolve_multicast_address(sys.argv[1], timeout=5.0)
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             struct.pack("=4s4s", socket.inet_aton(grp), socket.inet_aton("0.0.0.0")))
s.settimeout(0.5)
seen, end = {}, time.time() + 5
while time.time() < end:
    try:
        buf, _ = s.recvfrom(8192)
    except socket.timeout:
        continue
    st = decode_status_packet(buf)
    if st and st.frequency and round(st.frequency) in (28145000, 50260000):
        seen[(st.ssrc, round(st.frequency))] = (st.agc_enable, st.gain,
                                                st.frontend.rf_gain, st.frontend.rf_agc)
for k, v in sorted(seen.items()):
    print(k, "agc_enable=%s gain=%s fe_rf_gain=%s fe_rf_agc=%s" % v)
```

Expected: `agc_enable` False and `gain` 0.0 on both meteor channels.  Record the result in the
spec §3.1.  If AGC reads on, stop and tell Michael: the design assumption fails.

- [ ] **Step 2: Probe Zenodo's sandbox**

Michael creates a sandbox account and a personal access token at
`https://sandbox.zenodo.org/account/settings/applications/` (scopes `deposit:write` and
`deposit:actions`), and saves it to `~/.config/hamsci/zenodo-sandbox-token` (mode 0600).  Then:

```python
# zenodo_probe.py — read-only except one draft, deleted at the end
import json, sys, urllib.request
BASE = "https://sandbox.zenodo.org"
TOKEN = open(sys.argv[1]).read().strip()
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read() or b"null")
print(call("GET", "/api/communities?q=Ham%20Radio%20Science%20Citizen%20Investigation"))
st, draft = call("POST", "/api/records", {"metadata": {"title": "probe", "resource_type": {"id": "dataset"},
    "publication_date": "2026-10-17", "creators": [{"person_or_org": {"type": "personal",
    "given_name": "Probe", "family_name": "Test"}}]}, "files": {"enabled": True},
    "access": {"record": "public", "files": "public"}})
print(st, draft["id"], draft["links"].keys())
print(call("POST", f"/api/records/{draft['id']}/draft/pids/doi"))
print(call("DELETE", f"/api/records/{draft['id']}/draft"))
```

Run: `python3 -I zenodo_probe.py ~/.config/hamsci/zenodo-sandbox-token`
Record: whether `/api/records` (InvenioRDM) answers; the community's `id` on production (repeat
only the first GET against `https://zenodo.org`, no token needed); whether DOI reservation works on
a draft.  If the community search on production returns nothing, ask Michael for the community's
URL slug.

- [ ] **Step 3: Amend the spec**

Edit §3.1 (stream already f32; AGC/GAIN already sent; Step 1's result), §3.2 (uncalibrated bands
keep peak-normalization; the sidecar records `wav_gain_db` for every recording), §4 (own listener;
timing basis = host receive time of each status packet), §5.3 (Step 2's answers), §6 (MSQP state in
`inventory --json`; `smd status` rendering moved to a sigmond row).

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-17-msqp-capture-design.md
git commit -m "docs: MSQP spec — what the code and the Zenodo sandbox showed"
```

---

### Task 1: WAV writer with a fixed scale that reports what it did

**Files:**
- Modify: `src/meteor_scatter/core/wav.py`
- Test: `tests/test_wav.py`

**Interfaces:**
- Produces: `WavStats(gain_db: float, clipped_samples: int, noise_dbfs: Optional[float])`;
  `write_wav(path, samples, sample_rate, frequency_hz=0, scale_db: Optional[float] = None) -> WavStats`;
  `quiet_floor_dbfs(samples: np.ndarray, sample_rate: int) -> Optional[float]`.
  `gain_db` means: int16 value = float sample × 32767 × 10^(gain_db/20).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wav.py`)

```python
import numpy as np
from meteor_scatter.core.wav import write_wav, quiet_floor_dbfs, WavStats


class FixedScaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def _read_int16(self):
        return np.frombuffer(self.path.read_bytes()[44:], dtype="<i2")

    def test_fixed_scale_applies_exactly_and_reports_it(self):
        x = np.full(1200, 0.001, dtype=np.float32)
        st = write_wav(self.path, x, 12000, scale_db=20.0)
        self.assertEqual(st.gain_db, 20.0)
        self.assertEqual(int(self._read_int16()[0]), round(0.001 * 32767 * 10.0))
        self.assertEqual(st.clipped_samples, 0)

    def test_fixed_scale_counts_clipped_samples(self):
        x = np.array([0.5, -0.5, 0.01], dtype=np.float32)
        st = write_wav(self.path, x, 12000, scale_db=12.0)   # 0.5 * 3.98 > 1
        self.assertEqual(st.clipped_samples, 2)
        self.assertEqual(int(self._read_int16()[0]), 32767)
        self.assertEqual(int(self._read_int16()[1]), -32768)

    def test_peak_norm_reports_its_gain(self):
        x = np.array([0.0, 0.25, -0.1], dtype=np.float32)
        st = write_wav(self.path, x, 12000)
        expected = 20 * np.log10((32767.0 * 10 ** (-1 / 20) / 0.25) / 32767.0)
        self.assertAlmostEqual(st.gain_db, expected, places=6)
        self.assertEqual(st.clipped_samples, 0)

    def test_quiet_floor_ignores_a_loud_ping(self):
        rng = np.random.default_rng(1)
        x = (rng.standard_normal(12000 * 30) * 1e-3).astype(np.float32)
        x[12000:14400] += 0.3                      # a 200 ms ping
        floor = quiet_floor_dbfs(x, 12000)
        self.assertAlmostEqual(floor, -60.0, delta=1.5)

    def test_quiet_floor_of_silence_is_none(self):
        self.assertIsNone(quiet_floor_dbfs(np.zeros(12000, np.float32), 12000))
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_wav.py -v`
Expected: FAIL — `ImportError: cannot import name 'quiet_floor_dbfs'`.

- [ ] **Step 3: Implement** — replace the body of `core/wav.py` below the imports with:

```python
from dataclasses import dataclass
from typing import Optional

_FULL_SCALE = 32767.0


@dataclass(frozen=True)
class WavStats:
    """What write_wav did to one slot.  ``gain_db``: int16 = sample x 32767 x 10^(gain_db/20)."""
    gain_db: float
    clipped_samples: int
    noise_dbfs: Optional[float]


def quiet_floor_dbfs(samples: np.ndarray, sample_rate: int) -> Optional[float]:
    """The slot's noise floor: 10th percentile of 100 ms RMS blocks, dB re 1.0.

    The low percentile keeps a ping of up to a few seconds from raising it.
    None for an empty or all-zero slot.
    """
    block = max(1, sample_rate // 10)
    n = samples.size // block
    if n == 0:
        return None
    rms = np.sqrt(np.mean(samples[: n * block].astype(np.float64).reshape(n, block) ** 2, axis=1))
    floor = float(np.percentile(rms, 10))
    if floor <= 0.0:
        return None
    return 20.0 * np.log10(floor)


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    frequency_hz: int = 0,
    scale_db: Optional[float] = None,
) -> WavStats:
    """Write float32 samples as 16-bit PCM and report the gain applied.

    ``scale_db`` set: one fixed gain (a calibrated band).  ``None``: peak-normalize the slot to
    -1 dBFS, as before calibration existed.
    """
    gain_db = scale_db if scale_db is not None else _peak_norm_gain_db(samples)
    int16_samples, clipped = _to_int16(samples, gain_db)
    data_bytes = int16_samples.astype("<i2").tobytes()
    # (header packing and file write unchanged from the current function)
    ...
    return WavStats(gain_db=gain_db, clipped_samples=clipped,
                    noise_dbfs=quiet_floor_dbfs(samples, sample_rate))


_PEAK_TARGET_INT16 = _FULL_SCALE * (10.0 ** (-1.0 / 20.0))


def _peak_norm_gain_db(samples: np.ndarray) -> float:
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    if peak <= 0.0:
        return 0.0
    return 20.0 * np.log10((_PEAK_TARGET_INT16 / peak) / _FULL_SCALE)


def _to_int16(samples: np.ndarray, gain_db: float):
    scaled = samples.astype(np.float64) * (_FULL_SCALE * 10.0 ** (gain_db / 20.0))
    rounded = np.rint(scaled)
    clipped = int(np.count_nonzero((rounded > 32767.0) | (rounded < -32768.0)))
    return np.clip(rounded, -32768.0, 32767.0).astype(np.int16), clipped
```

Keep the existing RIFF header `struct.pack(...)` block, the `path.parent.mkdir` and file write, and
`_set_xattrs` exactly as they stand; the `...` above marks only that unchanged block.  Delete
`_float32_to_int16`.  Keep its comment about f32 channels, moved above `_peak_norm_gain_db`.

- [ ] **Step 4: Run the WAV tests, then the whole suite**

Run: `uv run pytest tests/test_wav.py -v` → PASS.
Run: `uv run pytest tests/ -q` → 302 passed, 1 skipped.  An existing test that compared bytes from
the old `_float32_to_int16` may now differ by one LSB through `rint`; if so, update its expectation
to the `np.rint` value and say so in the commit.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/core/wav.py tests/test_wav.py
git commit -m "wav: a fixed scale per band, and every write reports its gain, clipping and noise floor"
```

---

### Task 2: The archive

**Files:**
- Create: `src/meteor_scatter/core/archive.py`
- Test: `tests/test_archive.py`

**Interfaces:**
- Produces: `band_label(freq_hz: int) -> str`; `slot_stem(slot_start_utc: float) -> str`;
  `slot_day(slot_start_utc: float) -> datetime.date`;
  `class Archive(root: Path, max_days: int, max_gb: float)` with
  `keep(wav_path: Path, freq_hz: int, slot_start_utc: float, sidecar: dict) -> Path`,
  `prune(today: date) -> list[str]`, `usage(today: date) -> dict`,
  `recordings(first: date, last: date) -> list[tuple[str, Path, Path]]` (band, wav, json).

- [ ] **Step 1: Write the failing tests** (`tests/test_archive.py`)

```python
import json, tempfile, unittest
from datetime import date
from pathlib import Path

from meteor_scatter.core.archive import Archive, band_label, slot_stem, slot_day

T0 = 1797206370.0          # 2026-12-13 23:59:30 UTC


class NamingTests(unittest.TestCase):
    def test_band_labels(self):
        self.assertEqual(band_label(28145000), "10m")
        self.assertEqual(band_label(50260000), "6m")
        self.assertEqual(band_label(144150000), "144150kHz")

    def test_stem_follows_wsjtx(self):
        self.assertEqual(slot_stem(T0), "261213_235930")

    def test_slot_near_midnight_belongs_to_its_start_day(self):
        self.assertEqual(slot_day(T0), date(2026, 12, 13))


class KeepAndPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        self.spool = Path(self.tmp.name) / "spool"
        self.spool.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _wav(self, name="x.wav", size=1000):
        p = self.spool / name
        p.write_bytes(b"\0" * size)
        return p

    def test_keep_moves_wav_and_writes_sidecar(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        dest = a.keep(self._wav(), 28145000, T0, {"k": 1})
        self.assertEqual(dest, self.root / "10m" / "20261213" / "261213_235930.wav")
        self.assertTrue(dest.exists())
        self.assertEqual(json.loads(dest.with_suffix(".json").read_text()), {"k": 1})
        self.assertFalse((self.spool / "x.wav").exists())

    def test_prune_by_age(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        for d in ("20260901", "20261213"):
            (self.root / "6m" / d).mkdir(parents=True)
            (self.root / "6m" / d / "a.wav").write_bytes(b"\0")
        removed = a.prune(date(2026, 12, 14))
        self.assertEqual(removed, ["6m/20260901"])
        self.assertTrue((self.root / "6m" / "20261213").exists())

    def test_prune_by_size_removes_oldest_day_first(self):
        a = Archive(self.root, max_days=90, max_gb=0.000002)   # 2000 bytes
        for d in ("20261211", "20261212", "20261213"):
            (self.root / "10m" / d).mkdir(parents=True)
            (self.root / "10m" / d / "a.wav").write_bytes(b"\0" * 1000)
        removed = a.prune(date(2026, 12, 13))
        self.assertEqual(removed, ["10m/20261211"])

    def test_recordings_in_range_by_start_day(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        a.keep(self._wav("a.wav"), 50260000, T0, {})
        a.keep(self._wav("b.wav"), 50260000, T0 + 60, {})   # 2026-12-14 00:00:30
        got = a.recordings(date(2026, 12, 13), date(2026, 12, 13))
        self.assertEqual([p.name for _, p, _ in got], ["261213_235930.wav"])

    def test_usage_counts_bytes_oldest_and_today(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        a.keep(self._wav(size=500), 28145000, T0, {})
        u = a.usage(date(2026, 12, 13))
        self.assertEqual(u["oldest_day"], "20261213")
        self.assertEqual(u["kept_today"], 1)
        self.assertGreaterEqual(u["bytes"], 500)
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_archive.py -v` → FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** (`src/meteor_scatter/core/archive.py`)

```python
"""Decoded MSK144 recordings kept for the Meteor Scatter QSO Party.

Layout: <root>/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav plus a .json sidecar.  A slot belongs to the
UTC day it starts on.  The spool and the archive share /var/lib/meteor-scatter, so keep() moves a
recording with one rename.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BAND_NAMES = {28145000: "10m", 50260000: "6m"}


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
        """Move one decoded recording in and write its sidecar.  Raises OSError; leaves neither
        file half-written in the archive."""
        day_dir = self.root / band_label(freq_hz) / slot_day(slot_start_utc).strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / f"{slot_stem(slot_start_utc)}.wav"
        side = dest.with_suffix(".json")
        tmp = side.with_suffix(".json.part")
        try:
            tmp.write_text(json.dumps(sidecar, indent=1, sort_keys=True))
            try:
                os.replace(wav_path, dest)
            except OSError:
                shutil.move(str(wav_path), str(dest))
            os.replace(tmp, side)
        except OSError:
            for p in (tmp, dest):
                try:
                    p.unlink()
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
```

- [ ] **Step 4: Run** `uv run pytest tests/test_archive.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/core/archive.py tests/test_archive.py
git commit -m "archive: keep decoded recordings by band and start day, under an age and size cap"
```

---

### Task 3: The front-end gain log

**Files:**
- Create: `src/meteor_scatter/core/frontend.py`
- Test: `tests/test_frontend.py`

**Interfaces:**
- Produces: `FrontendReading(t, rf_gain, rf_atten, rf_agc, if_power, ad_over)` (frozen dataclass;
  `t` = host receive time, UTC seconds); `class FrontendLog(keep_sec=300.0, gap_sec=5.0)` with
  `add(r)`, `latest() -> Optional[FrontendReading]`,
  `window(t0: float, t1: float, sample_rate: int) -> dict`;
  `class FrontendListener(status_address: str, log: FrontendLog)` with `start()`, `stop()`.
  `window()` returns keys `basis`, `frontend_status` (`ok` | `partial` | `missing`), `start`, `end`,
  `steps` (list of `{from_db, to_db, atten_from, atten_to, sample_window: [a, b]}`),
  `missing_sample_windows` (list of `[a, b]`), `ad_over_delta`, `overloaded`.

- [ ] **Step 1: Write the failing tests** (`tests/test_frontend.py`)

```python
import unittest
from meteor_scatter.core.frontend import FrontendLog, FrontendReading

SR = 12000


def r(t, gain=10.0, atten=0.0, over=0):
    return FrontendReading(t=t, rf_gain=gain, rf_atten=atten, rf_agc=True,
                           if_power=-23.5, ad_over=over)


class WindowTests(unittest.TestCase):
    def test_steady_gain_has_no_steps(self):
        log = FrontendLog()
        for i in range(0, 40):
            log.add(r(1000.0 + i * 0.5))
        w = log.window(1005.0, 1015.0, SR)
        self.assertEqual(w["frontend_status"], "ok")
        self.assertEqual(w["steps"], [])
        self.assertEqual(w["start"]["rf_gain"], 10.0)

    def test_step_is_a_window_between_packets(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1004.5)); log.add(r(1005.0, gain=12.5)); log.add(r(1020.0, gain=12.5))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(len(w["steps"]), 1)
        s = w["steps"][0]
        self.assertEqual((s["from_db"], s["to_db"]), (10.0, 12.5))
        self.assertEqual(s["sample_window"], [int(4.5 * SR), int(5.0 * SR)])

    def test_gap_over_five_seconds_marks_missing(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1012.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "partial")
        self.assertIn([0, 12 * SR], w["missing_sample_windows"])

    def test_no_readings_at_all_is_missing(self):
        w = FrontendLog().window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "missing")
        self.assertIsNone(w["start"])

    def test_ad_over_rise_marks_overload(self):
        log = FrontendLog()
        log.add(r(999.0, over=7)); log.add(r(1010.0, over=7)); log.add(r(1012.0, over=9))
        w = log.window(1000.0, 1015.0, SR)
        self.assertEqual(w["ad_over_delta"], 2)
        self.assertTrue(w["overloaded"])

    def test_log_keeps_only_changes_or_one_per_second(self):
        log = FrontendLog()
        for i in range(100):
            log.add(r(1000.0 + i * 0.01))        # 100 packets in one second, no change
        self.assertLessEqual(len(log._readings), 2)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_frontend.py -v` → FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** (`src/meteor_scatter/core/frontend.py`)

```python
"""radiod's front-end gain, recorded beside each kept recording.

radiod's RX888 front-end AGC steps the VGA at most once a second, shared by every channel.  We do
not touch it; we record it.  FrontendListener joins radiod's status multicast group, sends nothing,
decodes each packet with ka9q.status.decode_status_packet and feeds FrontendLog.  A status packet
tells us the gain changed somewhere between two packets, not at which sample, so a step is reported
as a window of samples.  Times are the host receive time of each packet.
"""
from __future__ import annotations

import bisect
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

BASIS = "host receive time of radiod status packets"


@dataclass(frozen=True)
class FrontendReading:
    t: float
    rf_gain: Optional[float]
    rf_atten: Optional[float]
    rf_agc: Optional[bool]
    if_power: Optional[float]
    ad_over: Optional[int]

    def _state(self):
        return (self.rf_gain, self.rf_atten, self.rf_agc)


class FrontendLog:
    def __init__(self, keep_sec: float = 300.0, gap_sec: float = 5.0):
        self._keep = keep_sec
        self._gap = gap_sec
        self._readings: list[FrontendReading] = []
        self._lock = threading.Lock()

    def add(self, r: FrontendReading) -> None:
        with self._lock:
            last = self._readings[-1] if self._readings else None
            if last is not None and last._state() == r._state() and r.ad_over == last.ad_over \
                    and r.t - last.t < 1.0:
                return
            self._readings.append(r)
            cut = r.t - self._keep
            while self._readings and self._readings[0].t < cut:
                self._readings.pop(0)

    def latest(self) -> Optional[FrontendReading]:
        with self._lock:
            return self._readings[-1] if self._readings else None

    def window(self, t0: float, t1: float, sample_rate: int) -> dict:
        with self._lock:
            rs = list(self._readings)
        n = int(round((t1 - t0) * sample_rate))
        to_s = lambda t: max(0, min(n, int(round((t - t0) * sample_rate))))
        times = [x.t for x in rs]
        i0 = bisect.bisect_right(times, t0) - 1          # last reading at/before t0
        i1 = bisect.bisect_right(times, t1)              # first reading after t1
        span = rs[max(i0, 0): i1 + 1]
        if not span or (i0 < 0 and (i1 == 0)):
            return {"basis": BASIS, "frontend_status": "missing", "start": None, "end": None,
                    "steps": [], "missing_sample_windows": [[0, n]],
                    "ad_over_delta": None, "overloaded": None}
        before = rs[i0] if i0 >= 0 else None
        inside = [x for x in span if t0 < x.t <= t1]
        steps = []
        for a, b in zip(span, span[1:]):
            if a._state() != b._state() and b.t > t0 and a.t < t1:
                steps.append({"from_db": a.rf_gain, "to_db": b.rf_gain,
                              "atten_from": a.rf_atten, "atten_to": b.rf_atten,
                              "sample_window": [to_s(a.t), to_s(b.t)]})
        missing = []
        edges = ([t0] if before is None else [before.t]) + [x.t for x in inside] + [t1]
        for a, b in zip(edges, edges[1:]):
            if b - a > self._gap:
                missing.append([to_s(a), to_s(b)])
        start = before or (inside[0] if inside else None)
        end = inside[-1] if inside else before
        overs = [x.ad_over for x in ([before] if before else []) + inside if x.ad_over is not None]
        delta = (overs[-1] - overs[0]) if len(overs) >= 2 else None
        status = "ok" if not missing else "partial"
        return {"basis": BASIS, "frontend_status": status,
                "start": asdict(start) if start else None, "end": asdict(end) if end else None,
                "steps": steps, "missing_sample_windows": missing,
                "ad_over_delta": delta, "overloaded": (delta > 0) if delta is not None else None}


class FrontendListener:
    """Passive: joins the status group, decodes, never sends."""

    def __init__(self, status_address: str, log: FrontendLog):
        self._address = status_address
        self._log = log
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def _open(self) -> socket.socket:
        from ka9q.status_listener import DEFAULT_STATUS_PORT
        from ka9q.utils import resolve_multicast_address
        group = resolve_multicast_address(self._address, timeout=5.0)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("0.0.0.0", DEFAULT_STATUS_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")))
        s.settimeout(0.5)
        return s

    def start(self) -> None:
        self._sock = self._open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="frontend-listener")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()

    def _loop(self) -> None:
        from ka9q.status import decode_status_packet
        while self._running:
            try:
                buf, _ = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("frontend listener: %s", e)
                time.sleep(1.0)
                continue
            try:
                st = decode_status_packet(buf)
            except Exception:
                continue
            if st is None:
                continue
            fe = st.frontend
            if fe.rf_gain is None and fe.ad_over is None:
                continue
            self._log.add(FrontendReading(t=time.time(), rf_gain=fe.rf_gain,
                                          rf_atten=fe.rf_atten, rf_agc=fe.rf_agc,
                                          if_power=fe.if_power, ad_over=fe.ad_over))
```

- [ ] **Step 4: Run** `uv run pytest tests/test_frontend.py -v` → PASS.  If `test_no_readings_at_all`
or the gap test fails on the edge arithmetic, fix the code, not the test: the tests state the spec.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/core/frontend.py tests/test_frontend.py
git commit -m "frontend: record radiod's front-end gain steps as sample windows, passively"
```

---

### Task 4: The noise-floor watch

**Files:**
- Create: `src/meteor_scatter/core/noise_watch.py`
- Test: `tests/test_noise_watch.py`

**Interfaces:**
- Produces: `class NoiseWatch(calibrated_dbfs: Optional[float], threshold_db=10.0, hold_sec=3600.0)`
  with `observe(t: float, noise_dbfs: Optional[float], fe_gain_db: Optional[float]) -> Optional[str]`
  (a message only on a transition) and `state() -> dict` (`calibrated_dbfs`, `last_dbfs`,
  `alarmed`, `since`).  `calibrated_dbfs` = the band's noise in the f32 domain at calibration.

- [ ] **Step 1: Write the failing tests** (`tests/test_noise_watch.py`)

```python
import unittest
from meteor_scatter.core.noise_watch import NoiseWatch


class NoiseWatchTests(unittest.TestCase):
    def test_warns_once_after_an_hour_then_once_on_recovery(self):
        w = NoiseWatch(calibrated_dbfs=-60.0)
        msgs = [w.observe(t, -45.0, 10.0 + t / 3600) for t in range(0, 3601, 30)]
        self.assertEqual(sum(m is not None for m in msgs), 1)
        self.assertIn("15.0 dB", msgs[-1])
        self.assertIn("front-end gain", msgs[-1])
        self.assertTrue(w.state()["alarmed"])
        more = [w.observe(3601 + t, -45.0, 11.0) for t in range(0, 600, 30)]
        self.assertTrue(all(m is None for m in more))
        back = w.observe(5000, -61.0, 11.0)
        self.assertIsNotNone(back)
        self.assertFalse(w.state()["alarmed"])

    def test_a_short_excursion_never_warns(self):
        w = NoiseWatch(calibrated_dbfs=-60.0)
        msgs = [w.observe(t, -45.0, 10.0) for t in range(0, 1800, 30)]
        msgs.append(w.observe(1800, -60.0, 10.0))
        msgs += [w.observe(1830 + t, -45.0, 10.0) for t in range(0, 1800, 30)]
        self.assertTrue(all(m is None for m in msgs))

    def test_uncalibrated_never_warns(self):
        w = NoiseWatch(calibrated_dbfs=None)
        self.assertIsNone(w.observe(0, -10.0, 10.0))
        self.assertIsNone(w.observe(7200, -10.0, 10.0))
```

- [ ] **Step 2: Run** `uv run pytest tests/test_noise_watch.py -v` → FAIL.

- [ ] **Step 3: Implement** (`src/meteor_scatter/core/noise_watch.py`)

```python
"""Watch one band's noise floor against its calibrated level.  Warns; never adjusts."""
from __future__ import annotations

from typing import Optional


class NoiseWatch:
    def __init__(self, calibrated_dbfs: Optional[float], threshold_db: float = 10.0,
                 hold_sec: float = 3600.0):
        self._cal = calibrated_dbfs
        self._thr = threshold_db
        self._hold = hold_sec
        self._out_since: Optional[float] = None
        self._gain_at_out: Optional[float] = None
        self._alarmed = False
        self._last: Optional[float] = None

    def observe(self, t: float, noise_dbfs: Optional[float],
                fe_gain_db: Optional[float]) -> Optional[str]:
        if self._cal is None or noise_dbfs is None:
            return None
        self._last = noise_dbfs
        dev = noise_dbfs - self._cal
        if abs(dev) > self._thr:
            if self._out_since is None:
                self._out_since, self._gain_at_out = t, fe_gain_db
            if not self._alarmed and t - self._out_since >= self._hold:
                self._alarmed = True
                fe = ("front-end gain unknown" if fe_gain_db is None or self._gain_at_out is None
                      else f"front-end gain moved {fe_gain_db - self._gain_at_out:+.1f} dB")
                return (f"noise floor {noise_dbfs:.1f} dBFS sits {dev:+.1f} dB from its calibrated "
                        f"{self._cal:.1f} dBFS for an hour ({abs(dev):.1f} dB); {fe} over the same "
                        f"hour; re-run `meteor-scatter msqp calibrate` between showers")
            return None
        self._out_since = None
        if self._alarmed:
            self._alarmed = False
            return f"noise floor back within {self._thr:.0f} dB of calibration ({noise_dbfs:.1f} dBFS)"
        return None

    def state(self) -> dict:
        return {"calibrated_dbfs": self._cal, "last_dbfs": self._last,
                "alarmed": self._alarmed, "since": self._out_since if self._alarmed else None}
```

- [ ] **Step 4: Run** `uv run pytest tests/test_noise_watch.py -v` → PASS.  Check `"15.0 dB"`:
`dev` = +15.0, so the message carries `(15.0 dB)`.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/core/noise_watch.py tests/test_noise_watch.py
git commit -m "noise watch: warn once when a band's floor leaves calibration for an hour, once on return"
```

---

### Task 5: Config — `[msqp]` and per-band scales

**Files:**
- Modify: `src/meteor_scatter/config.py` (DEFAULTS, `load_config`, new `get_msqp`, `get_scale_db`)
- Modify: `config/meteor-scatter-config.toml.template` (commented `[msqp]` and `scale_db` example)
- Modify: `src/meteor_scatter/configurator.py` only if `config apply` rejects unknown sections —
  check `test_config_show_apply.py` patterns first
- Test: `tests/test_config.py`, `tests/test_config_show_apply.py`

**Interfaces:**
- Produces: `get_msqp(config: dict) -> dict` with keys `archive_max_days: int`, `archive_max_gb:
  float`, `operator: str`, `contact_email: str`, `antenna: str`, `comments: str`,
  `zenodo_url: str` (default `"https://zenodo.org"`), `zenodo_community: str` (default = the id
  from Task 0), `archive_dir: str` (default `"/var/lib/meteor-scatter/archive"`);
  `get_scale_db(radiod_block: dict, freq_hz: int) -> Optional[float]`;
  `get_noise_cal_dbfs(radiod_block, freq_hz) -> Optional[float]`.
  Calibration writes two values per band under `[radiod.msk144.scale_db]` and
  `[radiod.msk144.noise_cal_dbfs]`, both keyed by the frequency as a string.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
from meteor_scatter.config import get_msqp, get_scale_db, get_noise_cal_dbfs


class MsqpConfigTests(unittest.TestCase):
    def test_defaults(self):
        m = get_msqp({})
        self.assertEqual((m["archive_max_days"], m["archive_max_gb"]), (90, 2.0))
        self.assertEqual(m["operator"], "")
        self.assertEqual(m["zenodo_url"], "https://zenodo.org")

    def test_rejects_negative_cap(self):
        with self.assertRaises(ValueError):
            get_msqp({"msqp": {"archive_max_days": -1}})

    def test_scale_lookup_by_frequency(self):
        blk = {"msk144": {"freqs_hz": [28145000, 50260000],
                          "scale_db": {"28145000": 31.5},
                          "noise_cal_dbfs": {"28145000": -81.5}}}
        self.assertEqual(get_scale_db(blk, 28145000), 31.5)
        self.assertIsNone(get_scale_db(blk, 50260000))
        self.assertEqual(get_noise_cal_dbfs(blk, 28145000), -81.5)
```

Add to `tests/test_config_show_apply.py`, following `test_apply_deep_merges_with_existing`: an
apply carrying `{"msqp": {"operator": "AC0G"}}` round-trips through `config show`, and an apply
carrying `{"radiod": [... "msk144": {"scale_db": {"28145000": 31.5}}]}` preserves `freqs_hz`.

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_config_show_apply.py -v` → FAIL.

- [ ] **Step 3: Implement** in `config.py`:

```python
MSQP_DEFAULTS: dict[str, Any] = {
    "archive_dir": "/var/lib/meteor-scatter/archive",
    "archive_max_days": 90,
    "archive_max_gb": 2.0,
    "operator": "",
    "contact_email": "",
    "antenna": "",
    "comments": "",
    "zenodo_url": "https://zenodo.org",
    "zenodo_community": ZENODO_COMMUNITY_ID,   # constant set from Task 0's answer
}


def get_msqp(config: dict) -> dict:
    raw = dict(MSQP_DEFAULTS)
    raw.update(config.get("msqp", {}) or {})
    days, gb = raw["archive_max_days"], raw["archive_max_gb"]
    if not isinstance(days, int) or days < 1:
        raise ValueError(f"msqp.archive_max_days must be a positive int; got {days!r}")
    if not isinstance(gb, (int, float)) or gb <= 0:
        raise ValueError(f"msqp.archive_max_gb must be a positive number; got {gb!r}")
    raw["archive_max_gb"] = float(gb)
    return raw


def _per_band(radiod_block: dict, key: str, freq_hz: int) -> Optional[float]:
    table = (radiod_block.get("msk144", {}) or {}).get(key, {}) or {}
    v = table.get(str(int(freq_hz)))
    return float(v) if v is not None else None


def get_scale_db(radiod_block: dict, freq_hz: int) -> Optional[float]:
    return _per_band(radiod_block, "scale_db", freq_hz)


def get_noise_cal_dbfs(radiod_block: dict, freq_hz: int) -> Optional[float]:
    return _per_band(radiod_block, "noise_cal_dbfs", freq_hz)
```

Call `get_msqp(raw)` at the end of `load_config` so a bad value fails at load, as
`radiod_lifetime_frames` does.  Remove `"keep_wav": False` from `DEFAULTS["paths"]`; in
`load_config`, if `raw["paths"]` still carries `keep_wav`, log one warning: "paths.keep_wav retired:
meteor-scatter now keeps decoded recordings in msqp.archive_dir".  Add to the template:

```toml
# Meteor Scatter QSO Party archive (docs/OPERATIONS.md, "MSQP").
#[msqp]
#archive_max_days = 90
#archive_max_gb   = 2
#operator         = ""   # operator names and calls, for the Zenodo description
#contact_email    = ""
#antenna          = ""
#comments         = ""
```

- [ ] **Step 4: Run** the two test files → PASS; then `uv run pytest tests/ -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/config.py config/meteor-scatter-config.toml.template tests/test_config.py tests/test_config_show_apply.py
git commit -m "config: [msqp] block and per-band scale_db / noise_cal_dbfs; paths.keep_wav retires"
```

---

### Task 6: Wire it into the decoder path

**Files:**
- Create: `src/meteor_scatter/core/msqp.py` (the per-channel bundle and the sidecar builder)
- Modify: `src/meteor_scatter/core/slot.py` (replace `keep_wav`), `core/stream.py`,
  `core/receiver_manager.py` (`provision_channels`, `_provision_one_with_retry` ensure_channel
  call), `core/recorder.py` (build the runtime, hourly prune, minute state file)
- Test: `tests/test_slot.py`, `tests/test_msqp_runtime.py`

**Interfaces:**
- Consumes: `WavStats`, `write_wav(..., scale_db=)` (Task 1); `Archive`, `band_label` (Task 2);
  `FrontendLog` (Task 3); `NoiseWatch` (Task 4); `get_msqp`, `get_scale_db`, `get_noise_cal_dbfs`
  (Task 5).
- Produces:
  `class MsqpChannel(archive: Archive, frontend: FrontendLog, freq_hz: int, reporter_id: str,
  scale_db: Optional[float], noise: NoiseWatch, version: str, cadence_sec: float = 30.0)` with
  `on_decoded(wav_path: Path, slot_start_utc: float, sample_rate: int, stats: WavStats,
  decodes: list[dict]) -> bool` (True when archived), `on_slot(slot_start_utc, stats)` (noise watch,
  every slot), counters `archived`, `archive_failures`, and `state() -> dict`;
  `build_sidecar(**fields) -> dict`;
  `class MsqpRuntime(archive, frontend_log, listener)` with
  `channel(freq_hz, *, reporter_id, scale_db, noise_cal_dbfs, cadence_sec=30.0) -> MsqpChannel`,
  `start_listener(status_address)`, `prune(today)`, `write_state(path)`.
  `SlotWorker(..., msqp: Optional[MsqpChannel] = None)` replaces `keep_wav`.

- [ ] **Step 1: Write the failing SlotWorker tests** (append to `tests/test_slot.py`; change
`_make_worker` to take `msqp=None` in place of `keep_wav` and pass it through)

```python
from unittest import mock
from meteor_scatter.core.wav import WavStats


class _FakeMsqp:
    def __init__(self, fail=False):
        self.calls, self.slots, self.fail = [], [], fail
        self.scale_db = None
    def on_slot(self, slot_start, stats):
        self.slots.append(slot_start)
    def on_decoded(self, wav_path, slot_start, sr, stats, decodes):
        self.calls.append((wav_path, decodes))
        return not self.fail


class ArchiveDecisionTests(unittest.TestCase):
    LINE = "000000  -5  0.2 1500 & CQ AC0G EM38\n<DecodeFinished>\n"

    def _reap_with(self, stdout_text, msqp):
        with tempfile.TemporaryDirectory() as td:
            w, *_ = _make_worker(td, msqp=msqp)
            wav = Path(td) / "msk144" / "x.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"\0" * 64)
            w._slot_stats[wav] = WavStats(gain_db=30.0, clipped_samples=0, noise_dbfs=-80.0)
            proc = mock.MagicMock()
            proc.poll.return_value = 0
            proc.stdout.read.return_value = stdout_text.encode()
            proc.stderr.read.return_value = b""
            w._pending_procs = [(proc, wav, 1797206370.0, 0.0)]
            w._reap_finished()
            return wav.exists()

    def test_decoded_slot_is_offered_to_the_archive(self):
        m = _FakeMsqp()
        self._reap_with(self.LINE, m)
        self.assertEqual(len(m.calls), 1)
        self.assertEqual(m.calls[0][1][0]["message"], "CQ AC0G EM38")

    def test_sentinel_only_output_is_not_archived(self):
        m = _FakeMsqp()
        still_there = self._reap_with("<DecodeFinished>\n", m)
        self.assertEqual(m.calls, [])
        self.assertFalse(still_there)

    def test_archive_failure_deletes_and_counts(self):
        m = _FakeMsqp(fail=True)
        still_there = self._reap_with(self.LINE, m)
        self.assertFalse(still_there)

    def test_mutant_keep_everything_would_fail(self):
        # Guards the keep decision: a worker that archived every slot would call on_decoded here.
        m = _FakeMsqp()
        self._reap_with("", m)
        self.assertEqual(m.calls, [])
```

Then confirm the guard has teeth: temporarily change the archive condition in `_reap_finished` to
call `on_decoded` unconditionally, run `test_mutant_keep_everything_would_fail` and
`test_sentinel_only_output_is_not_archived`, watch both FAIL, and restore the condition.

- [ ] **Step 2: Write the failing MsqpChannel tests** (`tests/test_msqp_runtime.py`)

```python
import json, tempfile, unittest
from pathlib import Path
from meteor_scatter.core.archive import Archive
from meteor_scatter.core.frontend import FrontendLog, FrontendReading
from meteor_scatter.core.msqp import MsqpChannel
from meteor_scatter.core.noise_watch import NoiseWatch
from meteor_scatter.core.wav import WavStats

T0 = 1797206370.0


class MsqpChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fe = FrontendLog()
        self.fe.add(FrontendReading(T0 - 1, 10.0, 0.0, True, -23.5, 0))
        self.ch = MsqpChannel(archive=Archive(root / "a", 90, 2), frontend=self.fe,
                              freq_hz=50260000, reporter_id="AC0G/B4", scale_db=None,
                              noise=NoiseWatch(None), version="test")
        self.wav = root / "x.wav"
        self.wav.write_bytes(b"\0" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sidecar_carries_what_the_spec_lists(self):
        st = WavStats(gain_db=41.2, clipped_samples=3, noise_dbfs=-88.0)
        decodes = [{"snr": -5, "dt": 0.2, "audio_freq_hz": 1500, "sync": "&", "message": "CQ AC0G EM38"}]
        self.assertTrue(self.ch.on_decoded(self.wav, T0, 12000, st, decodes))
        side = json.loads((Path(self.tmp.name) / "a/6m/20261213/261213_235930.json").read_text())
        self.assertEqual(side["reporter_id"], "AC0G/B4")
        self.assertEqual(side["dial_freq_hz"], 50260000)
        self.assertEqual(side["scale"], "uncalibrated")
        self.assertEqual(side["wav_gain_db"], 41.2)
        self.assertEqual(side["clipped_samples"], 3)
        self.assertEqual(side["decodes"], decodes)
        self.assertEqual(side["channel_requested"]["agc_enable"], 0)
        self.assertIn("frontend_status", side["frontend"])
        self.assertEqual(side["software_version"], "test")

    def test_disk_full_counts_warns_once_and_recovers(self):
        st = WavStats(0.0, 0, -80.0)
        with self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            with unittest.mock.patch.object(self.ch._archive, "keep", side_effect=OSError(28, "No space")):
                self.assertFalse(self.ch.on_decoded(self.wav, T0, 12000, st, [{"message": "x"}]))
                self.assertFalse(self.ch.on_decoded(self.wav, T0 + 30, 12000, st, [{"message": "x"}]))
        self.assertEqual(self.ch.archive_failures, 2)
        self.assertEqual(sum("archiving stopped" in m for m in logs.output), 1)
        self.wav.write_bytes(b"\0" * 64)
        with self.assertLogs("meteor_scatter.core.msqp", level="INFO") as logs:
            self.assertTrue(self.ch.on_decoded(self.wav, T0 + 60, 12000, st, [{"message": "x"}]))
        self.assertTrue(any("archiving resumed" in m for m in logs.output))
```

(`import unittest.mock` at the top.)

- [ ] **Step 3: Run both files** → FAIL.

- [ ] **Step 4: Implement `core/msqp.py`**

```python
"""Per-channel MSQP glue: build the sidecar, keep the recording, watch the noise floor."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from meteor_scatter.core.archive import Archive, band_label
from meteor_scatter.core.frontend import FrontendLog
from meteor_scatter.core.noise_watch import NoiseWatch
from meteor_scatter.core.wav import WavStats

logger = logging.getLogger(__name__)

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
                 scale_db: Optional[float], noise: NoiseWatch, version: str):
        self._archive = archive
        self._frontend = frontend
        self.freq_hz = freq_hz
        self._reporter_id = reporter_id
        self.scale_db = scale_db
        self._noise = noise
        self._version = version
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
        cadence = 30.0
        frontend = self._frontend.window(slot_start_utc, slot_start_utc + cadence, sample_rate)
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
```

Pass the real cadence instead of the literal `30.0`: add `cadence_sec: float` to `MsqpChannel`'s
constructor (default 30.0) and use it in `on_decoded`; `ReceiverManager` passes `tr_period_sec`.

- [ ] **Step 5: Change `core/slot.py`**

1. Constructor: replace `keep_wav: bool = False` with `msqp=None`; store `self._msqp = msqp`;
   add `self._slot_stats: dict[Path, WavStats] = {}`.
2. `_write_spool_wav`: pass `scale_db=self._msqp.scale_db if self._msqp else None` to
   `write_wav`, keep the returned `WavStats` in `self._slot_stats[wav_path]`, and call
   `self._msqp.on_slot(slot_start_utc, stats)` when `self._msqp` is set.
3. `_materialise_jt9_output`: return the list of parsed decode dicts (`decode` objects that passed
   `parse_decoded_txt_line`), `[]` when none.  Existing callers ignore the return value.
4. In `_reap_finished` (ret == 0 branch) and `_reap_all(wait=True)`: capture
   `decodes = self._materialise_jt9_output(...)`.  Then replace every
   `if not self._keep_wav: wav_path.unlink(missing_ok=True)` with one helper:

```python
    def _finish_slot(self, wav_path: Path, slot_start: float, decodes: list) -> None:
        stats = self._slot_stats.pop(wav_path, None)
        if self._msqp is not None and decodes and stats is not None:
            if self._msqp.on_decoded(wav_path, slot_start, self._sr, stats, decodes):
                return                       # moved into the archive
        wav_path.unlink(missing_ok=True)
```

   On the timeout, launch-failure and non-zero-exit paths call `_finish_slot(wav_path, slot_start, [])`.
5. Delete `self._keep_wav`.  Update the module docstring's last paragraph: decoded recordings go to
   the MSQP archive (core/msqp.py); the rest are deleted.

- [ ] **Step 6: Thread it through `stream.py`, `receiver_manager.py`, `recorder.py`**

- `ChannelSink.__init__`: replace `keep_wav: bool = False` with `msqp=None`; pass `msqp=msqp` to
  `SlotWorker`.
- `ReceiverManager.provision_channels`: replace `keep_wav: bool` with `msqp_runtime=None`.  For each
  `freq_hz`, build
  `msqp = msqp_runtime.channel(freq_hz, reporter_id=self._reporter_id, scale_db=get_scale_db(self._radiod, freq_hz), noise_cal_dbfs=get_noise_cal_dbfs(self._radiod, freq_hz), cadence_sec=tr_period_sec) if msqp_runtime else None`
  and pass `msqp=msqp` to `ChannelSink`.  `self._reporter_id`: take it in `ReceiverManager.__init__`
  from the recorder (it already holds the config; use `config.extract_reporter_id(config) or
  radiod_id`).  Start the front-end listener here, beside the existing `StatusListener`, inside the
  same best-effort try: `msqp_runtime.start_listener(status)`.
- `_provision_one_with_retry`'s `self._control.ensure_channel(...)`: add `agc_enable=0, gain=0.0`.
  Add a test in `tests/test_receiver_manager.py` that asserts the call carries both.
- `MeteorScatterRecorder._provision_all_receivers`: drop `keep_wav = self._paths.get(...)`; build one
  `MsqpRuntime` per receiver from `get_msqp(self._config)` and the meteor-scatter version
  (`meteor_scatter.version.GIT_INFO`), and pass it.
- `_stats_loop`: once an hour call `runtime.prune(today)` and log each removed day at INFO
  ("archive: removed 6m/20260901 (age)"); every minute call
  `runtime.write_state(<spool>/<radiod_id>/msqp-state.json)` beside `_write_applied_state`,
  best-effort like it.

`MsqpRuntime` lives in `core/msqp.py`:

```python
class MsqpRuntime:
    def __init__(self, msqp_cfg: dict, version: str):
        self.archive = Archive(Path(msqp_cfg["archive_dir"]), msqp_cfg["archive_max_days"],
                               msqp_cfg["archive_max_gb"])
        self.frontend = FrontendLog()
        self._listener = None
        self._version = version
        self.channels: list[MsqpChannel] = []

    def start_listener(self, status_address: str) -> None:
        from meteor_scatter.core.frontend import FrontendListener
        try:
            self._listener = FrontendListener(status_address, self.frontend)
            self._listener.start()
        except Exception as e:                       # sidecars then say "missing"
            logger.warning("front-end listener unavailable: %s", e)

    def channel(self, freq_hz, *, reporter_id, scale_db, noise_cal_dbfs, cadence_sec=30.0):
        ch = MsqpChannel(self.archive, self.frontend, freq_hz, reporter_id, scale_db,
                         NoiseWatch(noise_cal_dbfs), self._version, cadence_sec=cadence_sec)
        self.channels.append(ch)
        return ch

    def prune(self, today):
        return self.archive.prune(today)

    def write_state(self, path: Path) -> None:
        fe = self.frontend.latest()
        state = {"written_at": time.time(), "archive": self.archive.usage(
                     datetime.now(timezone.utc).date()),
                 "frontend_last_packet_age_sec": (time.time() - fe.t) if fe else None,
                 "channels": [c.state() for c in self.channels]}
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
```

`write_state` computes `archive.usage()` by walking the archive.  At 2 GB of 720 KB files that runs
about 3,000 `stat` calls a minute; acceptable.  If the stats thread shows it, cache usage for ten
minutes.

- [ ] **Step 7: Run** `uv run pytest tests/test_slot.py tests/test_msqp_runtime.py tests/test_receiver_manager.py -v` → PASS; then `uv run pytest tests/ -q` → all pass.  Fix every
remaining `keep_wav` reference (`grep -rn keep_wav src tests`) — tests that passed `keep_wav=True`
to keep a WAV for inspection now pass `msqp=None` and read the WAV before reap.

- [ ] **Step 8: Commit**

```bash
git add src/meteor_scatter/core/ tests/
git commit -m "msqp: keep each decoded recording with its sidecar; explicit AGC off; hourly prune; state file"
```

---

### Task 7: `meteor-scatter msqp calibrate`

**Files:**
- Create: `src/meteor_scatter/msqp_cli.py` (all three `msqp` subcommands live here)
- Modify: `src/meteor_scatter/cli.py` (register `msqp` and dispatch)
- Test: `tests/test_msqp_cli.py`

**Interfaces:**
- Consumes: `msqp-state.json` from Task 6 is not enough (it holds only the last floor), so the
  daemon appends each slot's floor to a small rolling file:
  `<spool>/<radiod_id>/noise-<freq_hz>.txt`, one line `"<slot_utc> <noise_dbfs>"`, trimmed to the
  last 120 lines.  Add that to `MsqpChannel.on_slot` (path given at construction,
  `noise_log: Optional[Path]`).
- Produces: `calibrate_scale(floors: list[float], target_dbfs: float = -50.0) -> tuple[float, float]`
  returning `(scale_db, noise_cal_dbfs)`; `cmd_calibrate(args) -> int`.

- [ ] **Step 1: Failing tests** (`tests/test_msqp_cli.py`)

```python
import unittest
from meteor_scatter.msqp_cli import calibrate_scale


class CalibrateTests(unittest.TestCase):
    def test_puts_noise_fifty_db_below_full_scale(self):
        scale, cal = calibrate_scale([-82.0, -81.0, -80.0, -95.0, -60.0, -81.5, -80.5, -81.0, -82.0, -80.0])
        self.assertAlmostEqual(cal, -81.0, places=1)        # median, not mean
        self.assertAlmostEqual(scale, 31.0, places=1)       # -50 - (-81)

    def test_refuses_too_few_slots(self):
        with self.assertRaises(ValueError):
            calibrate_scale([-80.0] * 9)
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
MIN_SLOTS = 10          # five minutes at 30 s


def calibrate_scale(floors, target_dbfs=-50.0):
    vals = sorted(f for f in floors if f is not None)
    if len(vals) < MIN_SLOTS:
        raise ValueError(f"need {MIN_SLOTS} slots of noise measurements; have {len(vals)} — "
                         f"let the daemon run five minutes and try again")
    mid = len(vals) // 2
    cal = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return round(target_dbfs - cal, 1), round(cal, 1)
```

`cmd_calibrate(args)`: resolve the config with `resolve_config_path(args.instance, args.config)`,
read each band's `noise-<freq>.txt`, compute, print one line per band
(`10m: noise -81.0 dBFS → scale_db 31.0`), and write both tables through the same validated path
`config apply` uses (`configurator` exposes it; call it, do not re-implement the TOML write).  Print
at the end: "restart meteor-scatter@<instance> to apply; this restarts no radiod".  `--dry-run`
prints without writing.

- [ ] **Step 4: Register in `cli.py`**: `sub_msqp = subparsers.add_parser("msqp", help="Meteor Scatter QSO Party archive")`, with sub-subparsers `calibrate`, `package`, `draft` built by
`msqp_cli.add_subparsers(sub_msqp, common=_add_common)`; dispatch `elif args.command == "msqp": sys.exit(msqp_cli.dispatch(args))`.  Each subcommand takes `--instance`.

- [ ] **Step 5: Run** `uv run pytest tests/test_msqp_cli.py -v` → PASS, then the suite.

- [ ] **Step 6: Commit**

```bash
git add src/meteor_scatter/msqp_cli.py src/meteor_scatter/cli.py src/meteor_scatter/core/msqp.py tests/test_msqp_cli.py
git commit -m "msqp calibrate: set each band's scale from the noise the daemon already measures"
```

---

### Task 8: Package contents — title, ALL.TXT, description, metadata

**Files:**
- Create: `src/meteor_scatter/msqp_package.py`
- Test: `tests/test_msqp_package.py`

**Interfaces:**
- Produces: `title(callsign: str, grid: str, first: date, last: date) -> str`;
  `all_txt_line(sidecar: dict, decode: dict) -> str`;
  `description_fields(cfg_msqp: dict, callsign: str, grid: str, bands: list[str], version: str) -> list[tuple[str, str]]`;
  `description_text(fields) -> str` (plain, for `description.md`);
  `description_html(fields) -> str` (for Zenodo's description field);
  `record_metadata(title: str, description_html: str, operator: str, today: date) -> dict`
  (InvenioRDM draft body; Task 0 may adjust field names).

- [ ] **Step 1: Failing tests** (`tests/test_msqp_package.py`)

```python
import unittest
from datetime import date
from meteor_scatter import msqp_package as P


class TitleTests(unittest.TestCase):
    def test_msqp_form_same_month(self):
        self.assertEqual(P.title("AC0G/B4", "em38ww40pk", date(2026, 12, 12), date(2026, 12, 15)),
                         "AC0G/B4 EM38WW 20261212-15 Meteor Scatter QSO Party")

    def test_single_day_and_cross_month(self):
        self.assertEqual(P.title("AC0G", "EM38ww", date(2026, 12, 13), date(2026, 12, 13)),
                         "AC0G EM38WW 20261213 Meteor Scatter QSO Party")
        self.assertEqual(P.title("AC0G", "EM38ww", date(2026, 12, 31), date(2027, 1, 2)),
                         "AC0G EM38WW 20261231-0102 Meteor Scatter QSO Party")


class AllTxtTests(unittest.TestCase):
    def test_wsjtx_layout(self):
        side = {"slot_start_utc": "2026-12-13T23:59:30+00:00", "dial_freq_hz": 50260000}
        d = {"snr": -2, "dt": 0.3, "audio_freq_hz": 1500, "message": "CQ W3USR FN21"}
        self.assertEqual(P.all_txt_line(side, d),
                         "261213_235930    50.260 Rx MSK144     -2  0.3 1500 CQ W3USR FN21\n")


class DescriptionTests(unittest.TestCase):
    CFG = {"operator": "Michael Hauan AC0G", "contact_email": "a@b.c", "antenna": "dipole", "comments": ""}

    def test_fields_in_msqp_order_with_monitor_values(self):
        f = dict(P.description_fields(self.CFG, "AC0G/B4", "EM38ww40pk", ["10m", "6m"], "v1"))
        self.assertEqual(f["Operation Mode"], "Receive Only")
        self.assertEqual(f["Transmitter Output Power"], "NA")
        self.assertEqual(f["Six Character Grid Square"], "EM38WW")
        self.assertEqual(f["Bands Operated"], "10m, 6m")
        self.assertIn("AGC", f["Additional Comments"])
        self.assertIn("v1", f["Additional Comments"])
        keys = [k for k, _ in P.description_fields(self.CFG, "AC0G/B4", "EM38ww40pk", ["6m"], "v1")]
        self.assertEqual(keys, P.MSQP_FIELD_ORDER)

    def test_metadata_never_asks_to_publish(self):
        md = P.record_metadata("t", "<p>x</p>", "Michael Hauan AC0G", date(2026, 12, 16))
        self.assertEqual(md["metadata"]["resource_type"], {"id": "dataset"})
        self.assertEqual(md["access"]["record"], "public")
        self.assertEqual(md["metadata"]["rights"], [{"id": "cc-by-4.0"}])
        self.assertNotIn("submit", str(md).lower())
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** (`src/meteor_scatter/msqp_package.py`)

```python
"""What an MSQP submission holds (github.com/HamSCI/MSQP, Zenodo.md)."""
from __future__ import annotations

import html
from datetime import date, datetime

MSQP_FIELD_ORDER = [
    "Callsign Used (or PSKReporter Identifier)", "Operator Names and Calls", "Contact Email Address",
    "Six Character Grid Square", "Operation Mode", "Transceiver/Transmitter/Receiver Make(s) & Model(s)",
    "Transmitter Output Power", "Antenna", "Bands Operated", "Additional Comments",
]
EQUIPMENT = "RX888 MkII SDR with ka9q-radio radiod; decoded by WSJT-X jt9 (meteor-scatter)"


def grid6(grid: str) -> str:
    return grid.strip().upper()[:6]


def title(callsign: str, grid: str, first: date, last: date) -> str:
    if first == last:
        span = first.strftime("%Y%m%d")
    elif (first.year, first.month) == (last.year, last.month):
        span = f"{first.strftime('%Y%m%d')}-{last.strftime('%d')}"
    else:
        span = f"{first.strftime('%Y%m%d')}-{last.strftime('%m%d')}"
    return f"{callsign} {grid6(grid)} {span} Meteor Scatter QSO Party"


def all_txt_line(sidecar: dict, decode: dict) -> str:
    t = datetime.fromisoformat(sidecar["slot_start_utc"])
    mhz = sidecar["dial_freq_hz"] / 1e6
    return (f"{t.strftime('%y%m%d_%H%M%S')} {mhz:9.3f} Rx {'MSK144':<6} {int(decode['snr']):6d} "
            f"{float(decode['dt']):4.1f} {int(decode['audio_freq_hz']):4d} {decode['message']}\n")


def description_fields(cfg: dict, callsign: str, grid: str, bands: list, version: str):
    comment = ("Receive-only monitor.  Channel AGC off; each WAV carries one fixed gain, recorded "
               "with the RX888 front-end gain in the .json sidecar beside it (front-end steps given "
               f"as sample windows).  Software: meteor-scatter {version}.")
    if cfg.get("comments"):
        comment = f"{cfg['comments']}  {comment}"
    values = [callsign, cfg["operator"], cfg["contact_email"], grid6(grid), "Receive Only",
              EQUIPMENT, "NA", cfg["antenna"], ", ".join(bands), comment]
    return list(zip(MSQP_FIELD_ORDER, values))


def description_text(fields) -> str:
    return "".join(f"{k}: {v}\n" for k, v in fields)


def description_html(fields) -> str:
    return "".join(f"<p><strong>{html.escape(k)}:</strong> {html.escape(v)}</p>" for k, v in fields)


def record_metadata(title_text: str, desc_html: str, operator: str, today: date) -> dict:
    return {
        "access": {"record": "public", "files": "public"},
        "files": {"enabled": True},
        "metadata": {
            "resource_type": {"id": "dataset"},
            "title": title_text,
            "publication_date": today.isoformat(),
            "creators": [{"person_or_org": {"type": "personal", "name": operator,
                                            "family_name": operator}}],
            "description": desc_html,
            "rights": [{"id": "cc-by-4.0"}],
        },
    }
```

The creator's name splitting stays simple on purpose: `operator` may hold several names and calls.
Task 0's probe shows whether InvenioRDM accepts `family_name` alone; adjust there, not here.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/msqp_package.py tests/test_msqp_package.py
git commit -m "msqp package contents: MSQP title, WSJT-X ALL.TXT lines, the description template, draft metadata"
```

---

### Task 9 + 10: `meteor-scatter msqp package`

**Files:**
- Modify: `src/meteor_scatter/msqp_cli.py` (add `build_package` and `cmd_package`)
- Test: `tests/test_msqp_cli.py`

**Interfaces:**
- Consumes: `Archive.recordings` (Task 2); everything in `msqp_package` (Task 8); `get_msqp`
  (Task 5).
- Produces: `build_package(archive: Archive, cfg_msqp: dict, callsign: str, grid: str, first: date,
  last: date, out_root: Path, version: str) -> PackageResult` where `PackageResult` holds `dir:
  Path`, `zip_path: Path`, `files: int`, `per_band_day: dict[str, dict[str, int]]`,
  `flagged: list[str]` (clipped or overloaded recordings).  Raises `PackageRefused(message)`.

- [ ] **Step 1: Failing tests** (append to `tests/test_msqp_cli.py`)

```python
import json, tempfile, zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from meteor_scatter.core.archive import Archive
from meteor_scatter.msqp_cli import build_package, PackageRefused

CFG = {"operator": "Michael Hauan AC0G", "contact_email": "a@b.c", "antenna": "dipole", "comments": ""}


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Archive(self.root / "archive", 90, 2)
        for t, clip in ((1797206370.0, 0), (1797206430.0, 4)):     # 12-13 23:59:30, 12-14 00:00:30
            w = self.root / "w.wav"; w.write_bytes(b"\0" * 64)
            iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
            self.a.keep(w, 50260000, t, {"slot_start_utc": iso, "dial_freq_hz": 50260000,
                "clipped_samples": clip, "frontend": {"overloaded": False},
                "decodes": [{"snr": -2, "dt": 0.3, "audio_freq_hz": 1500, "message": "CQ X"}]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_zip_alltxt_description_and_metadata(self):
        r = build_package(self.a, CFG, "AC0G/B4", "EM38ww", date(2026, 12, 13), date(2026, 12, 14),
                          self.root / "msqp", "v1")
        self.assertEqual(r.dir.name, "AC0G-B4 EM38WW 20261213-14 Meteor Scatter QSO Party")
        with zipfile.ZipFile(r.zip_path) as z:
            self.assertEqual(sorted(z.namelist()), [
                "6m/20261213/261213_235930.json", "6m/20261213/261213_235930.wav",
                "6m/20261214/261214_000030.json", "6m/20261214/261214_000030.wav"])
        self.assertEqual(len((r.dir / "ALL.TXT").read_text().splitlines()), 2)
        self.assertTrue((r.dir / "description.md").exists())
        self.assertIn("title", json.loads((r.dir / "metadata.json").read_text())["metadata"])
        self.assertEqual(r.flagged, ["6m/20261214/261214_000030.wav"])

    def test_range_by_start_day(self):
        r = build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 12, 13), date(2026, 12, 13),
                          self.root / "msqp", "v1")
        self.assertEqual(r.files, 1)

    def test_refuses_reversed_range_and_writes_nothing(self):
        with self.assertRaises(PackageRefused):
            build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 12, 14), date(2026, 12, 13),
                          self.root / "msqp", "v1")
        self.assertFalse((self.root / "msqp").exists())

    def test_refuses_empty_range(self):
        with self.assertRaisesRegex(PackageRefused, "no recordings"):
            build_package(self.a, CFG, "AC0G", "EM38ww", date(2026, 8, 1), date(2026, 8, 2),
                          self.root / "msqp", "v1")

    def test_refuses_missing_operator_field_by_name(self):
        with self.assertRaisesRegex(PackageRefused, "contact_email"):
            build_package(self.a, dict(CFG, contact_email=""), "AC0G", "EM38ww",
                          date(2026, 12, 13), date(2026, 12, 14), self.root / "msqp", "v1")
```

The directory name replaces `/` in the callsign with `-` (a path cannot hold `/`); the title inside
`metadata.json` keeps `AC0G/B4`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `build_package`** in `msqp_cli.py`:

```python
import json, zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from meteor_scatter import msqp_package as P

REQUIRED_OPERATOR_FIELDS = ("operator", "contact_email", "antenna")


class PackageRefused(Exception):
    pass


@dataclass
class PackageResult:
    dir: Path
    zip_path: Path
    files: int
    per_band_day: dict = field(default_factory=dict)
    flagged: list = field(default_factory=list)


def build_package(archive, cfg, callsign, grid, first, last, out_root, version):
    if first > last:
        raise PackageRefused(f"--from {first} falls after --to {last}")
    missing = [k for k in REQUIRED_OPERATOR_FIELDS if not str(cfg.get(k, "")).strip()]
    if missing:
        raise PackageRefused("set these in the instance config's [msqp] block first: "
                             + ", ".join(missing))
    recs = archive.recordings(first, last)
    if not recs:
        raise PackageRefused(f"no recordings in the archive between {first} and {last}")
    title_text = P.title(callsign, grid, first, last)
    out = Path(out_root) / title_text.replace("/", "-")
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{out.name}.zip"
    lines, per, flagged, bands = [], {}, [], []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as z:
        for band, wav, side_path in recs:
            rel = wav.relative_to(archive.root)
            z.write(wav, str(rel))
            z.write(side_path, str(rel.with_suffix(".json")))
            side = json.loads(side_path.read_text())
            for d in side.get("decodes", []):
                lines.append(P.all_txt_line(side, d))
            day = rel.parts[1]
            per.setdefault(band, {}).setdefault(day, 0)
            per[band][day] += 1
            if side.get("clipped_samples") or (side.get("frontend") or {}).get("overloaded"):
                flagged.append(str(rel))
            if band not in bands:
                bands.append(band)
    (out / "ALL.TXT").write_text("".join(lines))
    fields = P.description_fields(cfg, callsign, grid, bands, version)
    (out / "description.md").write_text(P.description_text(fields))
    md = P.record_metadata(title_text, P.description_html(fields), cfg["operator"],
                           datetime.now(timezone.utc).date())
    (out / "metadata.json").write_text(json.dumps(md, indent=1))
    return PackageResult(dir=out, zip_path=zip_path, files=len(recs), per_band_day=per,
                         flagged=flagged)
```

`cmd_package(args)`: load config, `get_msqp`, callsign from `config.extract_reporter_id(cfg)` or
`[station] callsign`, grid from `[station] grid_square`, out root
`/var/lib/meteor-scatter/msqp`; call `build_package`; print per-band per-day counts, the file count,
the zip size, and each flagged recording; on `PackageRefused` print the message to stderr and
return 2.

- [ ] **Step 4: Run** `uv run pytest tests/test_msqp_cli.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/msqp_cli.py tests/test_msqp_cli.py
git commit -m "msqp package: zip a date range of recordings with ALL.TXT, description and draft metadata"
```

---

### Task 11: The Zenodo client

**Files:**
- Create: `src/meteor_scatter/zenodo.py`
- Test: `tests/test_zenodo.py`

**Interfaces:**
- Produces: `class ZenodoError(Exception)` (carries `status`, `body`);
  `class Zenodo(base_url: str, token: str, transport=None)` with
  `create_draft(metadata: dict) -> dict`, `draft_url(record: dict) -> str`,
  `upload(record_id: str, path: Path) -> None` (init, content, commit),
  `committed_files(record_id) -> set[str]`, `reserve_doi(record_id) -> None`,
  `request_community(record_id, community_id) -> None` (creates the review request, does not
  submit it), `get_draft(record_id) -> dict`.
  `transport(method, url, headers, data: bytes|None) -> (status: int, body: bytes)`; default uses
  `urllib.request`.  Paths follow InvenioRDM as Task 0 confirmed:
  `POST /api/records`, `POST /api/records/{id}/draft/files`,
  `PUT /api/records/{id}/draft/files/{key}/content`, `POST /api/records/{id}/draft/files/{key}/commit`,
  `GET /api/records/{id}/draft/files`, `POST /api/records/{id}/draft/pids/doi`,
  `PUT /api/records/{id}/draft/review` with
  `{"receiver": {"community": <id>}, "type": "community-submission"}`.
  The class has no method that calls `/actions/submit-review` or `/actions/publish`.

- [ ] **Step 1: Failing tests** (`tests/test_zenodo.py`)

```python
import json, tempfile, unittest
from pathlib import Path
from meteor_scatter.zenodo import Zenodo, ZenodoError


class FakeTransport:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        return self.replies.pop(0)


class ZenodoTests(unittest.TestCase):
    def test_create_sends_bearer_token_and_json(self):
        t = FakeTransport([(201, json.dumps({"id": "abc", "links": {"self_html": "https://z/abc"}}).encode())])
        z = Zenodo("https://sandbox.zenodo.org", "TOK", transport=t)
        rec = z.create_draft({"metadata": {}})
        self.assertEqual(rec["id"], "abc")
        m, url, h, data = t.calls[0]
        self.assertEqual((m, url), ("POST", "https://sandbox.zenodo.org/api/records"))
        self.assertEqual(h["Authorization"], "Bearer TOK")

    def test_upload_is_init_content_commit(self):
        t = FakeTransport([(201, b"{}"), (200, b"{}"), (200, b"{}")])
        z = Zenodo("https://z", "T", transport=t)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.zip"; p.write_bytes(b"PK")
            z.upload("abc", p)
        self.assertEqual([c[0] for c in t.calls], ["POST", "PUT", "POST"])
        self.assertTrue(t.calls[1][1].endswith("/api/records/abc/draft/files/a.zip/content"))
        self.assertEqual(t.calls[1][3], b"PK")

    def test_error_status_raises_with_body(self):
        t = FakeTransport([(403, b'{"message": "no"}')])
        with self.assertRaises(ZenodoError) as cm:
            Zenodo("https://z", "T", transport=t).create_draft({})
        self.assertEqual(cm.exception.status, 403)

    def test_no_publish_or_submit_anywhere(self):
        import meteor_scatter.zenodo as zmod
        src = Path(zmod.__file__).read_text()
        self.assertNotIn("submit-review", src)
        self.assertNotIn("/actions/publish", src)
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** (`src/meteor_scatter/zenodo.py`)

```python
"""Just enough of Zenodo's InvenioRDM API to leave an MSQP draft for its operator.

This module can create a draft, upload files, reserve a DOI and attach the community's review
request.  It has no way to submit the request or publish: a person does that on Zenodo, because a
published record can never change.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ZenodoError(Exception):
    def __init__(self, status, body, what):
        super().__init__(f"Zenodo {what}: HTTP {status}: {body[:300]!r}")
        self.status, self.body = status, body


def _urllib_transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Zenodo:
    def __init__(self, base_url: str, token: str, transport=None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._t = transport or _urllib_transport

    def _call(self, what, method, path, body=None, raw: bytes = None,
              content_type="application/json"):
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = content_type
        status, resp = self._t(method, self._base + path, headers, data)
        if status >= 300:
            raise ZenodoError(status, resp, what)
        return json.loads(resp) if resp else {}

    def create_draft(self, metadata: dict) -> dict:
        return self._call("create draft", "POST", "/api/records", metadata)

    @staticmethod
    def draft_url(record: dict) -> str:
        return record.get("links", {}).get("self_html", "")

    def get_draft(self, record_id: str) -> dict:
        return self._call("read draft", "GET", f"/api/records/{record_id}/draft")

    def committed_files(self, record_id: str) -> set:
        r = self._call("list files", "GET", f"/api/records/{record_id}/draft/files")
        return {e["key"] for e in r.get("entries", []) if e.get("status") == "completed"}

    def upload(self, record_id: str, path: Path) -> None:
        key = urllib.parse.quote(path.name)
        base = f"/api/records/{record_id}/draft/files"
        self._call("start upload", "POST", base, [{"key": path.name}])
        self._call("upload", "PUT", f"{base}/{key}/content", raw=path.read_bytes(),
                   content_type="application/octet-stream")
        self._call("commit upload", "POST", f"{base}/{key}/commit")

    def reserve_doi(self, record_id: str) -> None:
        self._call("reserve DOI", "POST", f"/api/records/{record_id}/draft/pids/doi")

    def request_community(self, record_id: str, community_id: str) -> None:
        self._call("attach community", "PUT", f"/api/records/{record_id}/draft/review",
                   {"receiver": {"community": community_id}, "type": "community-submission"})
```

`upload` reads the whole zip into memory.  A shower's zip runs to tens of megabytes, so that holds;
if a station ever kept gigabytes, stream the file instead.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/zenodo.py tests/test_zenodo.py
git commit -m "zenodo: create a draft, upload, reserve a DOI, attach the community — never submit or publish"
```

---

### Task 12: `meteor-scatter msqp draft`

**Files:**
- Modify: `src/meteor_scatter/msqp_cli.py` (add `site_upload_mode`, `read_token`, `run_draft`,
  `cmd_draft`)
- Test: `tests/test_msqp_cli.py`

**Interfaces:**
- Consumes: `Zenodo` (Task 11); the package directory from Task 10 (`metadata.json`, the zip,
  `ALL.TXT`).
- Produces: `site_upload_mode(path=Path("/etc/sigmond/coordination.toml")) -> str`;
  `read_token(path: Path) -> str` (raises `DraftRefused`); `class DraftRefused(Exception)`;
  `run_draft(pkg_dir: Path, zenodo: Zenodo, community_id: str, upload_mode: str) -> str` (returns
  the draft URL) keeping state in `<pkg_dir>/draft.json`:
  `{"record_id", "url", "uploaded": [names], "doi_reserved": bool, "community_requested": bool}`.

- [ ] **Step 1: Failing tests** (append to `tests/test_msqp_cli.py`)

```python
import os, stat
from meteor_scatter.msqp_cli import run_draft, read_token, site_upload_mode, DraftRefused


class FakeZenodo:
    def __init__(self, fail_upload_once=None):
        self.calls, self.fail = [], fail_upload_once
    def create_draft(self, md):
        self.calls.append(("create",)); return {"id": "r1", "links": {"self_html": "https://z/r1"}}
    def draft_url(self, rec): return rec["links"]["self_html"]
    def get_draft(self, rid): return {"id": rid, "links": {"self_html": "https://z/r1"}}
    def committed_files(self, rid): return {c[1] for c in self.calls if c[0] == "upload"}
    def upload(self, rid, p):
        if self.fail == p.name:
            self.fail = None; raise OSError("network")
        self.calls.append(("upload", p.name))
    def reserve_doi(self, rid): self.calls.append(("doi",))
    def request_community(self, rid, cid): self.calls.append(("community", cid))


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = Path(self.tmp.name) / "T"
        self.pkg.mkdir()
        (self.pkg / "metadata.json").write_text('{"metadata": {"title": "T"}}')
        (self.pkg / "T.zip").write_bytes(b"PK")
        (self.pkg / "ALL.TXT").write_text("x\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_run_order_and_no_publish(self):
        z = FakeZenodo()
        url = run_draft(self.pkg, z, "comm-id", "upload")
        self.assertEqual(url, "https://z/r1")
        self.assertEqual([c[0] for c in z.calls], ["create", "upload", "upload", "doi", "community"])

    def test_resume_after_failed_upload_does_not_duplicate(self):
        z = FakeZenodo(fail_upload_once="ALL.TXT")
        with self.assertRaises(OSError):
            run_draft(self.pkg, z, "c", "upload")
        run_draft(self.pkg, z, "c", "upload")
        self.assertEqual([c for c in z.calls if c[0] == "create"], [("create",)])
        self.assertEqual(sorted(c[1] for c in z.calls if c[0] == "upload"), ["ALL.TXT", "T.zip"])

    def test_refuses_unless_upload_mode(self):
        for mode in ("hold", "discard"):
            with self.assertRaisesRegex(DraftRefused, mode):
                run_draft(self.pkg, FakeZenodo(), "c", mode)

    def test_token_must_exist_and_be_private(self):
        p = Path(self.tmp.name) / "tok"
        with self.assertRaises(DraftRefused):
            read_token(p)
        p.write_text("abc\n"); os.chmod(p, 0o644)
        with self.assertRaisesRegex(DraftRefused, "0600"):
            read_token(p)
        os.chmod(p, 0o600)
        self.assertEqual(read_token(p), "abc")

    def test_site_mode_absent_file_means_upload(self):
        self.assertEqual(site_upload_mode(Path(self.tmp.name) / "none.toml"), "upload")

    def test_site_mode_reads_hold(self):
        p = Path(self.tmp.name) / "c.toml"
        p.write_text('[uploads]\nmode = "hold"\n')
        self.assertEqual(site_upload_mode(p), "hold")
        p.write_text('[uploads]\nenabled = false\n')
        self.assertEqual(site_upload_mode(p), "hold")
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** in `msqp_cli.py`:

```python
import tomllib

COORDINATION = Path("/etc/sigmond/coordination.toml")
TOKEN_PATH = Path("/etc/meteor-scatter/zenodo-token")


class DraftRefused(Exception):
    pass


def site_upload_mode(path: Path = COORDINATION) -> str:
    """The site sink switch, read the way sigmond's coordination.Uploads reads it.  The per-client
    sink state (tasks/plan-sink-control.md) replaces this function once sigmond ships it."""
    try:
        up = (tomllib.loads(path.read_text()).get("uploads") or {})
    except FileNotFoundError:
        return "upload"
    mode = str(up.get("mode", "")).lower()
    if mode in ("upload", "hold", "discard"):
        return mode
    return "upload" if up.get("enabled", True) else "hold"


def read_token(path: Path = TOKEN_PATH) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise DraftRefused(f"no Zenodo token at {path}; create one (docs/OPERATIONS.md, MSQP) "
                           f"and save it there, mode 0600")
    if st.st_mode & 0o077:
        raise DraftRefused(f"{path} is readable by others; chmod 0600 {path}")
    return path.read_text().strip()


def run_draft(pkg_dir: Path, zenodo, community_id: str, upload_mode: str) -> str:
    if upload_mode != "upload":
        raise DraftRefused(f"the site upload mode reads {upload_mode!r}; a Zenodo draft leaves "
                           f"the station, so it waits for `upload`")
    state_path = pkg_dir / "draft.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    save = lambda: state_path.write_text(json.dumps(state, indent=1))
    if "record_id" not in state:
        rec = zenodo.create_draft(json.loads((pkg_dir / "metadata.json").read_text()))
        state.update(record_id=rec["id"], url=zenodo.draft_url(rec))
        save()
    rid = state["record_id"]
    done = zenodo.committed_files(rid)
    zip_name = next(p.name for p in pkg_dir.glob("*.zip"))
    for name in (zip_name, "ALL.TXT"):
        if name not in done:
            zenodo.upload(rid, pkg_dir / name)
    if not state.get("doi_reserved"):
        zenodo.reserve_doi(rid); state["doi_reserved"] = True; save()
    if not state.get("community_requested"):
        zenodo.request_community(rid, community_id); state["community_requested"] = True; save()
    return state["url"]
```

`cmd_draft(args)`: resolve config and `get_msqp`; build `Zenodo(cfg["zenodo_url"],
read_token(), ...)`; call `run_draft(Path(args.package), zenodo, cfg["zenodo_community"],
site_upload_mode())`; print the URL and the next step ("open it, check it, press Submit for review;
a community administrator publishes").  `DraftRefused` → stderr, return 2.  `ZenodoError` → stderr
with the status and body, return 1; the next run resumes.

- [ ] **Step 4: Run** → PASS; then the suite.

- [ ] **Step 5: Commit**

```bash
git add src/meteor_scatter/msqp_cli.py tests/test_msqp_cli.py
git commit -m "msqp draft: leave a resumable Zenodo draft; refuse without upload mode or a private token"
```

- [ ] **Step 6: Sandbox end to end** (needs Michael's sandbox token from Task 0)

Build a package from a test archive in the scratchpad, then:
`METEOR_SCATTER_CONFIG=<scratch config with zenodo_url = "https://sandbox.zenodo.org"> uv run meteor-scatter msqp draft <pkg>` with the token copied to a scratch path the config names.
Open the URL; confirm the draft holds the zip and `ALL.TXT`, the title, a reserved DOI, and the
community request in "draft" (not submitted).  Then delete the draft on the sandbox.  Record the
result in the plan's progress notes.  (Add a `zenodo_token_path` key to `[msqp]` in Task 5 if the
fixed `/etc` path blocks this; default stays `/etc/meteor-scatter/zenodo-token`.)

---

### Task 13: Inventory, deploy, docs, and the sigmond row

**Files:**
- Modify: `src/meteor_scatter/contract.py` (inventory gains an `msqp` block per instance, read from
  `msqp-state.json`; stale over 5 minutes or absent reads as null, like `timing-authority.json`)
- Modify: `deploy.toml` (declare `/var/lib/meteor-scatter/archive` and `/var/lib/meteor-scatter/msqp`
  where it declares the spool, so sigmond's disk accounting sees them)
- Modify: `docs/OPERATIONS.md` (new section "MSQP"), `docs/CONFIG.md` (`[msqp]`, `scale_db`,
  `noise_cal_dbfs`; `keep_wav` retired), `docs/ARCHITECTURE.md` and `CLAUDE.md` (the "WAV spool
  deleted after decode" decision now reads: decoded recordings go to the MSQP archive)
- Modify (sigmond repo): `tasks/release-v373.md` — new candidate row: `smd status` renders
  meteor-scatter's inventory `msqp` block (archive size, kept today, noise vs calibration, front-end
  status, open warning)
- Test: `tests/test_contract.py`

- [ ] **Step 1: Failing test** — following the `timing_authority_applied` tests in
`tests/test_contract.py`: with a fresh `msqp-state.json` in the spool dir, `inventory --json` carries
`msqp.channels[0].band == "6m"`; with a file older than 300 s it carries `msqp: null`.

- [ ] **Step 2: Run** → FAIL.  **Step 3:** implement in `contract.py`.  **Step 4:** run → PASS.

- [ ] **Step 5: Write OPERATIONS.md "MSQP"** in Michael's plain style (short sentences, active
voice).  It covers, in order: what the station keeps and where; the cap; `msqp calibrate` and when
to re-run it; the noise warning and what to do about it; the `[msqp]` operator fields; getting a
Zenodo account (ORCID, GitHub, or an institutional email, per the MSQP's own procedure) and a
personal access token (Zenodo → Applications → Personal access tokens; scopes `deposit:write` and
`deposit:actions`; save to `/etc/meteor-scatter/zenodo-token`, mode 0600); `msqp package
--from/--to`; `msqp draft`; and the last step on Zenodo, "Submit for review".

- [ ] **Step 6: Run** `uv run pytest tests/ -q` → all pass.  Then `graphify update /home/mjh/hamsci/repos`.

- [ ] **Step 7: Commit** (meteor-scatter, then sigmond separately)

```bash
git add src/meteor_scatter/contract.py deploy.toml docs/ CLAUDE.md tests/test_contract.py
git commit -m "msqp: inventory reports the archive and noise watch; deploy declares the archive; docs"
```

---

### Task 14: Prove it on B4

Station work.  Read the claude-bus first; announce before deploying; keep a watcher running.

- [ ] **Step 1:** Post on the bus: deploying meteor-scatter MSQP capture to AC0G-B4, restarts
`meteor-scatter@*` only, no radiod change.
- [ ] **Step 2:** Deploy per `reference_hf_timestd_deploy_mechanics` practice for this client (git
fast-forward of `/opt/git/sigmond/meteor-scatter`, then restart the instance).  Never reinstall.
- [ ] **Step 3:** After five minutes, `sudo -u meteorscat meteor-scatter msqp calibrate --instance <id> --dry-run`;
check the numbers look sane (noise somewhere near −80 dBFS, scale near +30 dB); run it without
`--dry-run`; restart the instance.
- [ ] **Step 4:** An MSK144 test transmission on an antenna close enough to hear, as on 2026-07-24
(WSJT-X Mode → MSK144, not Q65).  Expected: a `.wav` and `.json` under
`/var/lib/meteor-scatter/archive/10m/<today>/`, the sidecar's `scale` = `calibrated`,
`frontend.frontend_status` = `ok`.
- [ ] **Step 5:** Next day: count front-end steps across the day's sidecars; confirm windows narrow
(about half a second) and that `inventory --json` shows the `msqp` block.
- [ ] **Step 6:** Post results on the bus.  Update v3.73 row #12's status.
