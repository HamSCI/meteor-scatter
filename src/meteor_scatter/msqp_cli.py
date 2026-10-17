"""`meteor-scatter msqp ...`: calibrate, package and draft for the MSQP capture.

Task 7 provides `calibrate`; `package` and `draft` register in add_subparsers() and dispatch()
as they arrive.
"""
from __future__ import annotations

import argparse
import copy
import sys
import tomllib
from pathlib import Path

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


def dispatch(args) -> int:
    sub = getattr(args, "msqp_command", None)
    if sub == "calibrate":
        return cmd_calibrate(args)
    print("usage: meteor-scatter msqp {calibrate} --instance <id>", file=sys.stderr)
    return 2
