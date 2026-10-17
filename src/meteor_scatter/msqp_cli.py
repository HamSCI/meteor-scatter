"""`meteor-scatter msqp ...`: calibrate, package and draft for the MSQP capture.

Task 7 provides `calibrate`; `package` and `draft` register in add_subparsers() and dispatch()
as they arrive.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
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


def build_package(archive, cfg, callsign, grid, first, last, out_root, version):
    """Zip a date range of one instance's archive with ALL.TXT, description and draft metadata.
    Every refusal comes before the first mkdir.  A rerun replaces the earlier files in the
    directory and leaves draft.json (the Zenodo draft's state) alone."""
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
        radiod_id = resolve_radiod_status(blocks[0]) if blocks else ""
        reporter = (extract_reporter_id(config)
                    or (os.environ.get("STATION_REPORTER_ID") or "").strip() or None)
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
