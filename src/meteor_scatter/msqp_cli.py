"""`meteor-scatter msqp ...`: calibrate, package and draft for the MSQP capture.

`calibrate` sets each band's scale, `package` builds the upload, and `draft` leaves it on
Zenodo as an unsubmitted, unpublished draft for the operator to review.
"""
from __future__ import annotations

import argparse
import copy
import errno
import shutil
import time
import json
import os
import re
import stat
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from meteor_scatter import msqp_package as P
from meteor_scatter.zenodo import Zenodo, ZenodoError

MIN_SLOTS = 10          # five minutes at 30 s
CALIBRATE_WINDOW_SEC = 600.0     # calibrate reads only the last ten minutes of the noise log
FUTURE_SLACK_SEC = 60.0          # a line stamped further ahead than this comes from a clock step


def _now() -> float:
    return time.time()


def calibrate_scale(floors, target_dbfs=-50.0):
    """(scale_db, noise_cal_dbfs) from a band's per-slot noise floors, by the median."""
    vals = sorted(f for f in floors if f is not None)
    if len(vals) < MIN_SLOTS:
        raise ValueError(f"need {MIN_SLOTS} slots of noise measurements from the last "
                         f"{CALIBRATE_WINDOW_SEC / 60:.0f} minutes; have {len(vals)} — "
                         f"let the daemon run five minutes and try again")
    mid = len(vals) // 2
    cal = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return round(target_dbfs - cal, 1), round(cal, 1)


def read_noise_log(path: Path) -> list[tuple[float, float]]:
    """(slot_utc, floor) pairs from one noise-<freq>.txt; a missing file or a malformed line
    yields nothing.  The spool belongs to the daemon's account and this may run as root, so a
    link or anything but a regular file (a FIFO would hang a plain open) also yields nothing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as f:
        try:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return []
            text = f.read().decode(errors="replace")
        except OSError:
            return []
    samples = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            samples.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return samples


def recent_floors(samples, now: float, window_sec: float = CALIBRATE_WINDOW_SEC):
    """The floors whose slot lies within the last window_sec before `now`, with the first and
    last slot used (None, None when none qualify).  The log keeps an hour and survives
    restarts, so older lines would describe the receiver before a change."""
    kept = sorted((t, f) for t, f in samples
                  if now - window_sec <= t <= now + FUTURE_SLACK_SEC)
    if not kept:
        return [], None, None
    return [f for _, f in kept], kept[0][0], kept[-1][0]


def _utc_hms(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


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


def _open_sub(name: str, dir_fd) -> int:
    """fd of directory `name` (a path, when dir_fd is None) opened without following a link."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"{Path(str(name)).name} is a symlink or not a directory")
        if e.errno == errno.ENOENT:
            raise ValueError(f"{Path(str(name)).name} is missing")
        raise ValueError(f"{Path(str(name)).name} cannot be opened ({e.strerror})")


class _Missing(ValueError):
    """_open_regular's refusal when the name does not exist (still a ValueError)."""


def _open_regular(name: str, dir_fd):
    """(fd, fstat) for a regular, singly-linked file `name` inside the directory dir_fd,
    opened without following a link, or ValueError(reason).  The caller owns the fd.
    O_NONBLOCK keeps a FIFO in the file's place from hanging the open; the S_ISREG check then
    refuses it.  On a regular file the flag changes nothing."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise _Missing(f"{name} is missing")
        if e.errno == errno.ELOOP:
            raise ValueError(f"{name} is a symlink")
        raise ValueError(f"{name} cannot be opened ({e.strerror})")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{name} is not a regular file")
        if st.st_nlink != 1:
            raise ValueError(f"{name} has {st.st_nlink} hard links")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _open_recording(root_fd: int, parts):
    """_open_regular for archive-relative `parts` (band, day, file), walking each directory
    from the root fd with O_NOFOLLOW so that no name is ever resolved twice."""
    held, cur = [], root_fd
    try:
        for part in parts[:-1]:
            cur = _open_sub(part, cur)
            held.append(cur)
        return _open_regular(parts[-1], cur)
    finally:
        for fd in held:
            os.close(fd)


def _check_recording(root_fd: int, parts):
    """(sidecar dict, ALL.TXT lines, sidecar bytes, wav identity) for one recording, or
    raise ValueError(reason).  The archive belongs to the daemon's account and this may run
    as root, so a link, a hard link or a swapped directory is never followed: every open walks
    from the archive's root fd.  The sidecar is parsed from the bytes read there, and the
    wav's (st_dev, st_ino) lets the zip pass confirm it reopens the same file."""
    fd, wst = _open_recording(root_fd, parts)
    os.close(fd)
    fd, _ = _open_recording(root_fd, parts[:-1] + (Path(parts[-1]).with_suffix(".json").name,))
    with os.fdopen(fd, "rb") as f:
        raw = f.read()
    try:
        side = json.loads(raw.decode())
    except ValueError as e:                  # UnicodeDecodeError is a ValueError
        raise ValueError(f"sidecar is not valid JSON ({e.__class__.__name__})")
    if not isinstance(side, dict):
        raise ValueError("sidecar is not a JSON object")
    missing = [k for k in SIDECAR_KEYS if k not in side]
    if missing:
        raise ValueError("sidecar lacks " + ", ".join(missing))
    if not isinstance(side["decodes"], list):
        raise ValueError("sidecar decodes is not a list")
    for d in side["decodes"]:
        msg = d.get("message") if isinstance(d, dict) else None
        if isinstance(msg, str) and msg != "".join(msg.splitlines()):
            raise ValueError("a decode message holds a line break")
    try:
        lines = [P.all_txt_line(side, d) for d in side["decodes"]]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"sidecar will not render to ALL.TXT ({e.__class__.__name__}: {e})")
    return side, lines, raw, (wst.st_dev, wst.st_ino)


def _open_dir(path, what: str, dir_fd=None) -> int:
    """fd of a directory opened without following a link, which this process's user owns and
    neither group nor others can write (the daemon's account must not be able to swap what
    root writes into).  ValueError names the reason."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP or e.errno == errno.ENOTDIR:
            raise ValueError(f"{what} is a symlink or not a directory; refusing to write there")
        raise ValueError(f"{what} cannot be opened ({e.strerror})")
    try:
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        os.close(fd)
        raise ValueError(f"{what} must be owned by the current user and not group- or "
                         "other-writable")
    return fd


def _write_atomic(dir_fd: int, name: str, writer):
    """writer(fileobj) fills a fresh temp file beside the target (by dir fd, never by path);
    os.replace puts it in place.  A failure removes the temp file and leaves any earlier
    target as it was."""
    tmp = f".{name}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644,
                 dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            writer(f)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def build_package(archive, cfg, callsign, grid, first, last, out_root, version):
    """Zip a date range of one instance's archive with ALL.TXT, description and draft metadata.
    A refusal for the date range, the operator fields or an empty or wholly unusable archive
    comes before any directory exists; once the package directory is open, a failure leaves
    earlier files whole and removes its own temp files.  Unsafe or unreadable recordings are skipped
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
    try:
        arch_fd = _open_sub(str(archive.root), None)
    except ValueError as e:
        raise PackageRefused(f"archive root {archive.root}: {e}")
    try:
        return _package(archive, arch_fd, recs, cfg, callsign, grid, first, last, out_root,
                        version)
    finally:
        os.close(arch_fd)


def _package(archive, arch_fd, recs, cfg, callsign, grid, first, last, out_root, version):
    valid, lines, per, flagged, bands, skipped = [], [], {}, [], [], []
    for band, wav, side_path in recs:
        rel = wav.relative_to(archive.root)
        try:
            if len(rel.parts) != 3:
                raise ValueError("not at <band>/<day>/<file>")
            side, side_lines, raw, ident = _check_recording(arch_fd, rel.parts)
        except ValueError as e:
            skipped.append(f"{rel}: {e}")
            continue
        valid.append((raw, ident, rel))
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
    name = title_text.replace("/", "-")
    out = Path(out_root) / name                  # for reporting; never used for I/O
    zip_path = out / f"{name}.zip"
    old_umask = os.umask(0o022)          # new directories come out 0755 whatever the login umask
    try:
        os.makedirs(out_root, mode=0o755, exist_ok=True)
    finally:
        os.umask(old_umask)
    try:
        root_fd = _open_dir(out_root, f"output root {out_root}")
        try:
            old_umask = os.umask(0o022)
            try:
                os.mkdir(name, 0o755, dir_fd=root_fd)
            except FileExistsError:
                pass
            finally:
                os.umask(old_umask)
            pkg_fd = _open_dir(name, f"package directory {out}", dir_fd=root_fd)
        finally:
            os.close(root_fd)
    except ValueError as e:
        raise PackageRefused(str(e))

    def write_zip(f):
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_STORED) as z:
            for raw, ident, rel in valid:
                try:
                    fd, st = _open_recording(arch_fd, rel.parts)
                except ValueError as e:
                    raise PackageRefused(f"{rel} changed during packaging: {e}")
                with os.fdopen(fd, "rb") as src:
                    if (st.st_dev, st.st_ino) != ident:
                        raise PackageRefused(f"{rel} changed during packaging")
                    info = zipfile.ZipInfo(str(rel), time.localtime(st.st_mtime)[:6])
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = 0o644 << 16
                    with z.open(info, "w", force_zip64=True) as dst:
                        shutil.copyfileobj(src, dst)
                z.writestr(str(rel.with_suffix(".json")), raw,
                           compress_type=zipfile.ZIP_STORED)

    fields = P.description_fields(cfg, callsign, grid, bands, version)
    md = P.record_metadata(title_text, P.description_html(fields), cfg["operator"],
                           datetime.now(timezone.utc).date())
    try:
        _write_atomic(pkg_fd, zip_path.name, write_zip)
        _write_atomic(pkg_fd, "ALL.TXT", lambda f: f.write("".join(lines).encode()))
        _write_atomic(pkg_fd, "description.md",
                      lambda f: f.write(P.description_text(fields).encode()))
        _write_atomic(pkg_fd, "metadata.json",
                      lambda f: f.write(json.dumps(md, indent=1).encode()))
    finally:
        os.close(pkg_fd)
    return PackageResult(dir=out, zip_path=zip_path, files=len(valid), per_band_day=per,
                         flagged=flagged, skipped=skipped)


def _parse_day(text, flag):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise PackageRefused(f"{flag} wants YYYY-MM-DD; got {text!r}")


def _check_instance_matches(instance, reporter, source, *, per_instance: bool) -> None:
    """Refuse when --instance names a reporter id other than the one the environment gives.
    An instance names a reporter id when it holds "=" or "/", or when it selected a
    per-instance config; a legacy instance named after its radiod names none."""
    from meteor_scatter._reporter_id import to_storage
    if not instance or not reporter or not (per_instance or "=" in instance or "/" in instance):
        return

    def norm(x):
        try:
            return to_storage(x)
        except ValueError:
            return x
    if norm(instance) != norm(reporter):
        raise PackageRefused(
            f"--instance {instance} names a different reporter id than STATION_REPORTER_ID "
            f"{reporter} (from {source}); the daemon for that instance archives under the "
            f"latter.  Pass the matching --instance, or set [instance] reporter_id in the "
            f"instance config")


def cmd_package(args) -> int:
    from meteor_scatter.config import (PER_INSTANCE_CONFIG_DIR, get_msqp, load_config,
                                       resolve_config_path, resolve_msqp_instance_id,
                                       resolve_radiod_status, resolve_reporter_id)
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
        # The daemon's rule (config.resolve_reporter_id).  Under sudo the variable is gone,
        # so the files the unit loads for this instance supply it.
        reporter, source = resolve_reporter_id(config, instance=args.instance)
        if source not in (None, "config"):
            _check_instance_matches(instance, reporter, source,
                                    per_instance=(config_path == PER_INSTANCE_CONFIG_DIR
                                                  / f"{instance}.toml"))
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
        if os.path.realpath(out_root) != os.path.abspath(out_root):
            raise PackageRefused(f"{out_root} passes through a symlink; give the real path")
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


COORDINATION = Path("/etc/sigmond/coordination.toml")
UPLOAD_MODES = ("upload", "hold", "discard")
COMMUNITY_UNKNOWN = ("the MSQP community's Zenodo id is unknown until it is set in the instance "
                     "config's [msqp] zenodo_community; nothing was sent")


class DraftRefused(Exception):
    pass


def site_upload_mode(path: Path = COORDINATION) -> str:
    """The site sink switch, read the way sigmond's coordination.Uploads reads it: [uploads]
    mode, else [uploads] enabled (false → hold); an absent file means upload.  An unreadable or
    malformed file raises (OSError, ValueError) rather than defaulting to upload.  The per-client
    sink state (tasks/plan-sink-control.md) replaces this function once sigmond ships it."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return "upload"
    up = tomllib.loads(text).get("uploads") or {}
    if not isinstance(up, dict):
        raise ValueError(f"{path}: [uploads] is not a table")
    mode = str(up.get("mode", "")).lower()
    if mode in UPLOAD_MODES:
        return mode
    return "upload" if up.get("enabled", True) else "hold"


def _require_upload_mode(upload_mode: str) -> None:
    if upload_mode != "upload":
        raise DraftRefused(f"the site upload mode reads {upload_mode!r}; a Zenodo draft leaves "
                           f"the station, so it waits for `upload`")


def read_token(path: Path) -> str:
    """The Zenodo token, from a regular file only its owner can reach.  The file is opened once
    without following a link, and the mode check uses that open fd, so the check and the read
    concern the same file."""
    try:
        # O_NONBLOCK: a FIFO in the token's place must not hang the open; the S_ISREG check
        # then refuses it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise DraftRefused(f"no Zenodo token at {path}; create one (docs/OPERATIONS.md, MSQP) "
                           f"and save it there, mode 0600")
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise DraftRefused(f"{path} is a symlink; put the token file itself there, mode 0600")
        raise DraftRefused(f"{path} cannot be opened ({e.strerror})")
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise DraftRefused(f"{path} is not a regular file; save the token there, mode 0600")
        if st.st_mode & 0o077:
            raise DraftRefused(f"{path} has mode {stat.S_IMODE(st.st_mode):04o}, which lets "
                               f"group or others reach it; chmod 0600 {path}")
        data = f.read()
    try:
        token = data.decode().strip()
    except UnicodeDecodeError:
        raise DraftRefused(f"{path} does not hold a text token")
    if not token:
        raise DraftRefused(f"{path} is empty; save the Zenodo token there, mode 0600")
    return token


def run_draft(pkg_dir: Path, zenodo, community_id: str, upload_mode: str) -> str:
    """Leave the package at `pkg_dir` on Zenodo as a draft: create it, upload the zip and
    ALL.TXT, reserve the DOI and attach the community's review request, unsubmitted.  Returns
    the draft's URL.  Progress lives in <pkg_dir>/draft.json, so a re-run after any failure
    resumes without a second draft or a duplicate upload.

    Root owns the package directory and the msqp/ root above it.  The daemon's account owns
    /var/lib/meteor-scatter above both, so it can rename msqp/ aside and put something else in
    its place.  The directory is therefore opened once (no link, owner and mode checked), and
    every file inside is reached through that fd without following a link: never by pathname
    after the open."""
    _require_upload_mode(upload_mode)
    if not community_id:
        raise DraftRefused(COMMUNITY_UNKNOWN)
    pkg_dir = Path(os.path.abspath(pkg_dir))
    try:
        pkg_fd = _open_dir(str(pkg_dir), f"package directory {pkg_dir}")
    except ValueError as e:
        raise DraftRefused(str(e))
    try:
        return _draft(pkg_fd, pkg_dir, zenodo, community_id)
    finally:
        os.close(pkg_fd)


def _draft(pkg_fd: int, pkg_dir: Path, zenodo, community_id: str) -> str:
    zip_name = f"{pkg_dir.name}.zip"            # the name `msqp package` gives it
    held = {}
    try:
        try:
            for name in (zip_name, "ALL.TXT"):
                fd, _ = _open_regular(name, pkg_fd)
                held[name] = os.fdopen(fd, "rb")
            fd, _ = _open_regular("metadata.json", pkg_fd)
            with os.fdopen(fd, "rb") as f:
                md_raw = f.read()
        except ValueError as e:
            raise DraftRefused(f"{pkg_dir}: {e}; build it with `meteor-scatter msqp package`")
        try:
            md = json.loads(md_raw.decode())
        except ValueError:
            md = None
        if not isinstance(md, dict):
            raise DraftRefused(f"{pkg_dir}: metadata.json is not a JSON object; rebuild the "
                               "package")
        state = _read_state(pkg_fd, pkg_dir)

        def save():
            _write_atomic(pkg_fd, "draft.json",
                          lambda f: f.write(json.dumps(state, indent=1).encode()))

        if "record_id" not in state:
            rec = zenodo.create_draft(md)
            rid = rec.get("id") if isinstance(rec, dict) else None
            if not rid:
                raise ZenodoError(0, json.dumps(rec).encode(), "create draft (reply lacks an id)")
            state.update(record_id=rid, url=zenodo.draft_url(rec), uploaded=[],
                         doi_reserved=False, community_requested=False)
            save()
        rid = state["record_id"]
        state.setdefault("uploaded", [])
        done = zenodo.committed_files(rid)
        for name in (zip_name, "ALL.TXT"):
            if name not in done:
                zenodo.upload_bytes(rid, name, held[name].read())
            if name not in state["uploaded"]:
                state["uploaded"].append(name)
                save()
        if not state.get("doi_reserved"):
            zenodo.reserve_doi(rid)
            state["doi_reserved"] = True
            save()
        if not state.get("community_requested"):
            zenodo.request_community(rid, community_id)
            state["community_requested"] = True
            save()
        if not state.get("url"):
            state["url"] = zenodo.draft_url(zenodo.get_draft(rid))
            save()
        return state["url"]
    finally:
        for f in held.values():
            f.close()


def _read_state(pkg_fd: int, pkg_dir: Path) -> dict:
    """draft.json through the package fd: {} when absent; DraftRefused when it is a link, not
    a regular file, or not a state object.  A bad file stays where it is: it names a draft that
    already exists, and removing it makes the next run create a second one."""
    keep = ("it names the Zenodo draft this package already has; fix it by hand, or move it "
            "aside to start a new draft")
    try:
        fd, _ = _open_regular("draft.json", pkg_fd)
    except _Missing:
        return {}
    except ValueError as e:
        raise DraftRefused(f"{pkg_dir}: {e}")
    with os.fdopen(fd, "rb") as f:
        raw = f.read()
    try:
        state = json.loads(raw.decode())
    except ValueError:
        raise DraftRefused(f"{pkg_dir}: draft.json is not valid JSON; {keep}")
    if not isinstance(state, dict) or ("record_id" in state and (
            not state["record_id"] or not isinstance(state["record_id"], (str, int))
            or not isinstance(state.get("url", ""), str)
            or not isinstance(state.get("uploaded", []), list))):
        raise DraftRefused(f"{pkg_dir}: draft.json does not hold a draft's state; {keep}")
    return state


def cmd_draft(args) -> int:
    from meteor_scatter._reporter_id import unescape_systemd_instance
    from meteor_scatter.config import get_msqp, load_config, resolve_config_path

    token = None
    try:
        instance = unescape_systemd_instance(args.instance) if args.instance else None
        config = load_config(resolve_config_path(instance=instance, explicit_path=args.config))
        cfg = get_msqp(config)
        community = cfg["zenodo_community"].strip()
        if not community:
            raise DraftRefused(COMMUNITY_UNKNOWN)
        try:
            mode = site_upload_mode(COORDINATION)
        except (OSError, ValueError) as e:
            raise DraftRefused(f"cannot read the site upload mode from {COORDINATION}: {e}")
        _require_upload_mode(mode)
        pkg = Path(args.package)
        if os.path.realpath(pkg) != os.path.abspath(pkg):
            raise DraftRefused(f"{pkg} passes through a symlink; give the real path")
        token = read_token(Path(cfg["zenodo_token_path"]))
        url = run_draft(pkg, Zenodo(cfg["zenodo_url"], token), community, mode)
    except DraftRefused as e:
        print(f"msqp draft: {e}", file=sys.stderr)
        return 2
    except ZenodoError as e:
        # str(e) carries the step, the HTTP status (0: no answer) and a body excerpt
        msg = f"msqp draft: {'could not reach Zenodo: ' if e.status == 0 else ''}{e}"
        if e.status == 404:
            msg += ("\nif the draft was deleted on Zenodo, move the package's draft.json aside "
                    "to start a new one")
        msg += "\nprogress is saved in the package's draft.json; a re-run resumes from there"
        if token:
            msg = msg.replace(token, "<token>")
        print(msg, file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        msg = f"msqp draft: {e}"
        if token:
            msg = msg.replace(token, "<token>")
            msg += "\nprogress is saved in the package's draft.json; a re-run resumes from there"
        print(msg, file=sys.stderr)
        return 1 if token else 2
    print(f"draft: {url}")
    print("next: open it, check it, press Submit for review; a community administrator "
          "publishes")
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
    now = _now()
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
            floors, first_t, last_t = recent_floors(
                read_noise_log(spool / radiod_id / f"noise-{int(freq)}.txt"), now)
            try:
                scale, cal = calibrate_scale(floors)
            except ValueError as e:
                print(f"{label}: {e}")
                continue
            print(f"{label}: noise {cal:.1f} dBFS → scale_db {scale:.1f} "
                  f"({len(floors)} slots, {_utc_hms(first_t)}–{_utc_hms(last_t)} UTC)")
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
    expected = dict(raw)
    expected["radiod"] = raw_blocks if isinstance(raw.get("radiod"), list) else raw_blocks[0]
    try:
        patched = _patch_band_tables(config_path.read_text(encoding="utf-8"), raw_blocks, expected)
    except (OSError, UnicodeDecodeError):
        patched = None
    if patched is not None:
        rc = _write_patched(config_path, patched, load_config)
    else:
        # A shape the in-place patch does not handle (an inline table, dotted keys):
        # the validated rewrite, which loses the file's comments.
        print("msqp calibrate: could not patch the config in place; rewriting it, "
              "which drops its comments", file=sys.stderr)
        rc = configurator.apply_payload(config_path, {"radiod": raw_blocks})
    if rc != 0:
        return rc
    shown = args.instance or ""
    print(f"restart meteor-scatter@{shown} to apply; this restarts no radiod")
    return 0


_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\[\]#]+?)\s*\]\]?\s*(?:#.*)?$")
_BAND_TABLES = ("scale_db", "noise_cal_dbfs")


def _header_name(line: str):
    """(dotted name with whitespace and quotes removed, is_array) for a table header, else None."""
    m = _HEADER_RE.match(line)
    if not m:
        return None
    name = ".".join(part.strip().strip('"\'') for part in m.group(2).split("."))
    return name, m.group(1) == "[["


def _patch_band_tables(text: str, blocks: list, expected: dict):
    """``text`` with each [[radiod]] block's [radiod.msk144.scale_db] and
    [radiod.msk144.noise_cal_dbfs] entries set from ``blocks``, every other line untouched.

    Changes a value in place (keeping a trailing comment), adds a missing key after the
    table's last key, and adds a missing table at the end of its block.  Returns None when
    the result would not parse to ``expected``: a shape this does not handle, which the
    caller must not write."""
    lines = text.splitlines(keepends=True)
    headers = [(i, h) for i, h in ((i, _header_name(ln)) for i, ln in enumerate(lines)) if h]
    # [[radiod]] (or a lone [radiod]) opens a block; a header outside radiod.* closes it
    spans, start = [], None
    for i, (name, _arr) in headers:
        if name == "radiod":
            if start is not None:
                spans.append((start, i))
            start = i
        elif start is not None and not name.startswith("radiod."):
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(lines)))
    if len(spans) != len(blocks):
        return None

    edits = []          # (line index, "replace" | "insert", new text), applied bottom-up
    for (b0, b1), block in zip(spans, blocks):
        msk = block.get("msk144") or {}
        inner = [(i, name) for i, (name, _a) in headers if b0 < i < b1]
        for table in _BAND_TABLES:
            values = msk.get(table) or {}
            if not values:
                continue
            want = f"radiod.msk144.{table}"
            at = [i for i, name in inner if name == want]
            if len(at) > 1:
                return None
            if not at:
                end = b1
                while end - 1 > b0 and (not lines[end - 1].strip()
                                        or lines[end - 1].lstrip().startswith("#")):
                    end -= 1
                body = "".join(f'"{k}" = {_toml_float(v)}\n' for k, v in values.items())
                lead = "" if lines[end - 1].endswith("\n") else "\n"
                tail = "\n" if end < len(lines) and lines[end].strip() else ""
                edits.append((end, "insert", f"{lead}\n[{want}]\n{body}{tail}"))
                continue
            h = at[0]
            t_end = next((i for i, _n in inner if i > h), b1)
            last_kv = h
            for k, v in values.items():
                key_re = re.compile(r"^(\s*(?:\"%s\"|'%s'|%s)\s*=\s*)([^#\r\n]*?)(\s*(?:#.*)?)(\r?\n?)$"
                                    % ((re.escape(k),) * 3))
                hit = None
                for j in range(h + 1, t_end):
                    if key_re.match(lines[j]):
                        hit = j
                        break
                if hit is not None:
                    m = key_re.match(lines[hit])
                    if m.group(2).strip() != _toml_float(v):
                        edits.append((hit, "replace",
                                      m.group(1) + _toml_float(v) + m.group(3) + m.group(4)))
            for j in range(h + 1, t_end):
                if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                    last_kv = j
            missing = [k for k in values
                       if not any(re.match(r"^\s*(?:\"%s\"|'%s'|%s)\s*=" % ((re.escape(k),) * 3),
                                           lines[j]) for j in range(h + 1, t_end))]
            if missing:
                lead = "" if lines[last_kv].endswith("\n") else "\n"
                edits.append((last_kv + 1, "insert",
                              lead + "".join(f'"{k}" = {_toml_float(values[k])}\n' for k in missing)))
    # bottom-up, and at one index the later edit first, so the file reads in edit order
    for _seq, (idx, kind, new) in sorted(enumerate(edits), key=lambda e: (e[1][0], e[0]),
                                         reverse=True):
        if kind == "replace":
            lines[idx] = new
        else:
            lines.insert(idx, new)
    out = "".join(lines)
    try:
        if tomllib.loads(out) != expected:
            return None
    except tomllib.TOMLDecodeError:
        return None
    return out


def _toml_float(v) -> str:
    s = repr(float(v))
    return s if any(c in s for c in ".eEn") else s + ".0"


def _write_patched(config_path: Path, text: str, load_config) -> int:
    """Validate ``text`` through load_config, then replace ``config_path`` with it atomically,
    keeping the file's mode."""
    tmp = config_path.with_name(config_path.name + ".part")
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            shutil.copymode(config_path, tmp)
        except OSError:
            pass
        try:
            load_config(tmp)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            print(f"msqp calibrate: rejected by load_config: {e}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            return 2
        tmp.replace(config_path)
    except OSError as e:
        print(f"msqp calibrate: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return 1
    print(f"wrote {config_path}")
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

    dr = msqp_sub.add_parser(
        "draft",
        help="leave a package on Zenodo as an unsubmitted draft for you to check and submit")
    dr.add_argument("package", help="package directory that `msqp package` printed")
    dr.add_argument("--instance", default=None,
                    help="instance whose config holds [msqp] (selects "
                         "/etc/meteor-scatter/<instance>.toml)")
    if common:
        common(dr)


def dispatch(args) -> int:
    sub = getattr(args, "msqp_command", None)
    if sub == "calibrate":
        return cmd_calibrate(args)
    if sub == "package":
        return cmd_package(args)
    if sub == "draft":
        return cmd_draft(args)
    print("usage: meteor-scatter msqp {calibrate,package,draft} ...", file=sys.stderr)
    return 2
