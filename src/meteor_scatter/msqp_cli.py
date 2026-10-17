"""`meteor-scatter msqp ...`: calibrate, package and draft for the MSQP capture.

Task 7 provides `calibrate`; `package` and `draft` register in add_subparsers() and dispatch()
as they arrive.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from meteor_scatter import msqp_package as P

MIN_SLOTS = 10          # five minutes at 30 s


def calibrate_scale(floors, target_dbfs=-50.0):
    """(scale_db, noise_cal_dbfs) from a band's per-slot noise floors, by the median."""
    vals = sorted(f for f in floors if f is not None)
    if len(vals) < MIN_SLOTS:
        raise ValueError(f"need {MIN_SLOTS} slots of noise measurements; have {len(vals)} — "
                         f"let the daemon run five minutes and try again")
    mid = len(vals) // 2
    cal = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return round(target_dbfs - cal, 1), round(cal, 1)


def read_noise_log(path: Path) -> list[float]:
    """The floors in one noise-<freq>.txt; missing file or malformed lines yield nothing."""
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    floors = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            floors.append(float(parts[1]))
        except ValueError:
            continue
    return floors


REQUIRED_OPERATOR_FIELDS = ("operator", "contact_email", "antenna")
DEFAULT_OUT_ROOT = Path("/var/lib/meteor-scatter/msqp")


class PackageRefused(Exception):
    pass


@dataclass
class PackageResult:
    dir: Path
    zip_path: Path
    files: int
    per_band_day: dict = field(default_factory=dict)
    flagged: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


SIDECAR_KEYS = ("slot_start_utc", "dial_freq_hz", "decodes")


def _check_recording(root: Path, wav: Path, side_path: Path):
    """(sidecar dict, ALL.TXT lines) for one recording, or raise ValueError(reason).  The
    archive belongs to the daemon's account and this may run as root, so a link or a path
    outside the archive is never read."""
    for p in (wav, side_path):
        try:
            st = p.lstat()
        except OSError:
            raise ValueError(f"{p.name} is missing")
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"{p.name} is a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{p.name} is not a regular file")
        if not p.resolve().is_relative_to(root):
            raise ValueError(f"{p.name} resolves outside the archive")
    try:
        side = json.loads(side_path.read_text())
    except (OSError, ValueError) as e:       # UnicodeDecodeError is a ValueError
        raise ValueError(f"sidecar is not valid JSON ({e.__class__.__name__})")
    if not isinstance(side, dict):
        raise ValueError("sidecar is not a JSON object")
    missing = [k for k in SIDECAR_KEYS if k not in side]
    if missing:
        raise ValueError("sidecar lacks " + ", ".join(missing))
    if not isinstance(side["decodes"], list):
        raise ValueError("sidecar decodes is not a list")
    try:
        lines = [P.all_txt_line(side, d) for d in side["decodes"]]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"sidecar will not render to ALL.TXT ({e.__class__.__name__}: {e})")
    return side, lines


def _write_atomic(out: Path, name: str, writer):
    """writer(fileobj) fills a fresh temp file beside the target; os.replace puts it in place.
    A failure removes the temp file and leaves any earlier target as it was."""
    tmp = out / f".{name}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            writer(f)
        os.replace(tmp, out / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_package(archive, cfg, callsign, grid, first, last, out_root, version):
    """Zip a date range of one instance's archive with ALL.TXT, description and draft metadata.
    Every refusal comes before the first mkdir.  Unsafe or unreadable recordings are skipped
    and reported.  Each output lands by temp file + os.replace, so a failure leaves any
    earlier package whole.  A rerun leaves draft.json (the Zenodo draft's state) alone."""
    if first > last:
        raise PackageRefused(f"--from {first} falls after --to {last}")
    missing = [k for k in REQUIRED_OPERATOR_FIELDS if not str(cfg.get(k, "")).strip()]
    if missing:
        raise PackageRefused("set these in the instance config's [msqp] block first: "
                             + ", ".join(missing))
    recs = archive.recordings(first, last)
    if not recs:
        raise PackageRefused(f"no recordings in the archive between {first} and {last}")
    root = Path(archive.root).resolve()
    valid, lines, per, flagged, bands, skipped = [], [], {}, [], [], []
    for band, wav, side_path in recs:
        rel = wav.relative_to(archive.root)
        try:
            side, side_lines = _check_recording(root, wav, side_path)
        except ValueError as e:
            skipped.append(f"{rel}: {e}")
            continue
        valid.append((wav, side_path, rel))
        lines.extend(side_lines)
        day = rel.parts[1]
        per.setdefault(band, {}).setdefault(day, 0)
        per[band][day] += 1
        if side.get("clipped_samples") or (side.get("frontend") or {}).get("overloaded"):
            flagged.append(str(rel))
        if band not in bands:
            bands.append(band)
    if not valid:
        raise PackageRefused("no usable recordings between " f"{first} and {last}; skipped: "
                             + "; ".join(skipped))
    title_text = P.title(callsign, grid, first, last)
    out = Path(out_root) / title_text.replace("/", "-")
    if out.is_symlink():
        raise PackageRefused(f"{out} is a symlink; refusing to write through it")
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{out.name}.zip"

    def write_zip(f):
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_STORED) as z:
            for wav, side_path, rel in valid:
                z.write(wav, str(rel))
                z.write(side_path, str(rel.with_suffix(".json")))

    fields = P.description_fields(cfg, callsign, grid, bands, version)
    md = P.record_metadata(title_text, P.description_html(fields), cfg["operator"],
                           datetime.now(timezone.utc).date())
    _write_atomic(out, zip_path.name, write_zip)
    _write_atomic(out, "ALL.TXT", lambda f: f.write("".join(lines).encode()))
    _write_atomic(out, "description.md", lambda f: f.write(P.description_text(fields).encode()))
    _write_atomic(out, "metadata.json", lambda f: f.write(json.dumps(md, indent=1).encode()))
    return PackageResult(dir=out, zip_path=zip_path, files=len(valid), per_band_day=per,
                         flagged=flagged, skipped=skipped)


def _parse_day(text, flag):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise PackageRefused(f"{flag} wants YYYY-MM-DD; got {text!r}")


def cmd_package(args) -> int:
    from meteor_scatter.config import (extract_reporter_id, get_msqp, load_config,
                                       resolve_config_path, resolve_msqp_instance_id,
                                       resolve_radiod_status)
    from meteor_scatter._reporter_id import unescape_systemd_instance
    from meteor_scatter.core.archive import Archive, instance_dirname
    from meteor_scatter.core.recorder import _software_version

    try:
        first = _parse_day(args.date_from, "--from")
        last = _parse_day(args.date_to, "--to")
        instance = unescape_systemd_instance(args.instance) if args.instance else None
        config_path = resolve_config_path(instance=instance, explicit_path=args.config)
        config = load_config(config_path)
        cfg = get_msqp(config)
        blocks = config.get("radiod", [])
        if isinstance(blocks, dict):
            blocks = [blocks]
        reporter = (extract_reporter_id(config)
                    or (os.environ.get("STATION_REPORTER_ID") or "").strip() or None)
        radiod_id = ""
        if len(blocks) == 1 or (blocks and reporter):
            radiod_id = resolve_radiod_status(blocks[0])
        elif blocks:
            names = [resolve_radiod_status(b) for b in blocks]
            if instance in names:
                radiod_id = instance
            else:
                raise PackageRefused(
                    "the config has several [[radiod]] blocks and no reporter id; pass "
                    f"--instance set to one of: {', '.join(names)}")
        instance_id = resolve_msqp_instance_id(config, reporter, radiod_id)
        station = config.get("station", {}) or {}
        callsign = reporter or station.get("callsign", "") or instance_id
        grid = station.get("grid_square", "")
        if not grid:
            raise PackageRefused("set [station] grid_square in the instance config first")
        archive = Archive(Path(cfg["archive_dir"]) / instance_dirname(instance_id),
                          cfg["archive_max_days"], cfg["archive_max_gb"])
        out_root = Path(args.out) if args.out else DEFAULT_OUT_ROOT
        r = build_package(archive, cfg, callsign, grid, first, last, out_root,
                          _software_version())
    except (PackageRefused, OSError, ValueError) as e:
        print(f"msqp package: {e}", file=sys.stderr)
        return 2
    for band in sorted(r.per_band_day):
        for day in sorted(r.per_band_day[band]):
            print(f"{band} {day}: {r.per_band_day[band][day]} recordings")
    print(f"{r.files} recordings; {r.zip_path.stat().st_size / 1e6:.1f} MB zip")
    for note in r.skipped:
        print(f"skipped: {note}")
    for rel in r.flagged:
        print(f"flagged (clipped or overloaded): {rel}")
    print(f"package: {r.dir}")
    return 0


def cmd_calibrate(args) -> int:
    from meteor_scatter import configurator
    from meteor_scatter._reporter_id import unescape_systemd_instance
    from meteor_scatter.config import load_config, resolve_config_path, resolve_radiod_status
    from meteor_scatter.core.archive import band_label

    instance = unescape_systemd_instance(args.instance) if args.instance else None
    config_path = resolve_config_path(instance=instance, explicit_path=args.config)
    try:
        config = load_config(config_path)
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError) as e:
        print(f"msqp calibrate: {e}", file=sys.stderr)
        return 2

    spool = Path(config.get("paths", {}).get("spool_dir", "/var/lib/meteor-scatter"))
    raw_blocks = copy.deepcopy(raw.get("radiod", []))
    if isinstance(raw_blocks, dict):
        raw_blocks = [raw_blocks]
    many = len(raw_blocks) > 1
    done = 0
    for block in raw_blocks:
        msk = block.get("msk144", {}) or {}
        freqs = list(msk.get("freqs_hz", []))
        if not freqs:
            continue
        radiod_id = resolve_radiod_status(block)
        scales = dict(msk.get("scale_db", {}) or {})
        cals = dict(msk.get("noise_cal_dbfs", {}) or {})
        for freq in freqs:
            label = band_label(freq)
            if many:
                label = f"{radiod_id} {label}"
            floors = read_noise_log(spool / radiod_id / f"noise-{int(freq)}.txt")
            try:
                scale, cal = calibrate_scale(floors)
            except ValueError as e:
                print(f"{label}: {e}")
                continue
            print(f"{label}: noise {cal:.1f} dBFS → scale_db {scale:.1f}")
            scales[str(int(freq))] = scale
            cals[str(int(freq))] = cal
            done += 1
        if scales or cals:
            msk["scale_db"], msk["noise_cal_dbfs"] = scales, cals
            block["msk144"] = msk

    if not done:
        print("msqp calibrate: no band had enough noise measurements; nothing written",
              file=sys.stderr)
        return 2
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    rc = configurator.apply_payload(config_path, {"radiod": raw_blocks})
    if rc != 0:
        return rc
    shown = args.instance or ""
    print(f"restart meteor-scatter@{shown} to apply; this restarts no radiod")
    return 0


def add_subparsers(sub_msqp: argparse.ArgumentParser, *, common=None) -> None:
    """Register the msqp subcommands.  `common(sub)` adds --config and --log-level."""
    msqp_sub = sub_msqp.add_subparsers(dest="msqp_command")

    cal = msqp_sub.add_parser(
        "calibrate",
        help="set each band's scale_db from the noise floor the daemon has measured")
    cal.add_argument("--instance", required=True,
                     help="instance to calibrate (selects /etc/meteor-scatter/<instance>.toml)")
    cal.add_argument("--dry-run", action="store_true",
                     help="print the values; write nothing")
    if common:
        common(cal)

    pkg = msqp_sub.add_parser(
        "package",
        help="zip a date range of recordings with ALL.TXT, description and draft metadata")
    pkg.add_argument("--instance", required=True,
                     help="instance to package (selects /etc/meteor-scatter/<instance>.toml)")
    pkg.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD",
                     help="first start day (UTC)")
    pkg.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD",
                     help="last start day (UTC)")
    pkg.add_argument("--out", default=None, help=f"output root (default {DEFAULT_OUT_ROOT})")
    if common:
        common(pkg)


def dispatch(args) -> int:
    sub = getattr(args, "msqp_command", None)
    if sub == "calibrate":
        return cmd_calibrate(args)
    if sub == "package":
        return cmd_package(args)
    print("usage: meteor-scatter msqp {calibrate,package} --instance <id>", file=sys.stderr)
    return 2
