"""jt9 MSK144 decoder helpers — binary resolution, invocation, output parsing.

The recorder forks ``jt9`` once per 15 s slot in MSK144 mode.  Unlike
ka9q/ft8_lib's ``decode_ft8`` (which streams WSJT-X-style lines to
stdout), ``jt9`` writes its decodes to a **``decoded.txt``** file in its
``-a`` data directory and prints only a ``<DecodeFinished> <ndecodes> …``
sentinel to stdout.  Empirically confirmed 2026-06-12:

    $ jt9 --msk144 -p 15 -f 1500 -a <wd> <wav>     # cwd=<wd>
    <DecodeFinished>   0   0        0               # stdout
    # <wd>/decoded.txt  ← decode lines land here (one per ping decode)

So the slot worker reads the *delta* appended to ``decoded.txt`` after
each jt9 run (the same line-count-diff pattern wspr-recorder uses for
``fst4_decodes.dat``), and this module turns each raw jt9 line into a
normalized per-mode-log line the ChTailer parser consumes.

Binaries are bundled in-repo at ``bin/decoders/jt9-{x86,arm32,arm64}-v*``
and arch-resolved at runtime (mirrors wspr-recorder's
``_resolve_decoder_binaries``); an explicit config path overrides that.
"""

from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# WSJT-X MSK144 conventions.
MSK144_TR_PERIOD_SEC = 15          # standard meteor-scatter T/R sequence length
MSK144_AUDIO_FREQ_HZ = 1500        # default audio Tx passband centre (jt9 -f)
# The MSK144 sync indicator jt9 writes in the mode column (FT8=`~`, FT4=`+`,
# JT65=`#`, JT9=`@`, MSK144=`&`).  Used both to tag normalized log lines and
# to detect them on the parse side.
MSK144_SYNC_CHAR = "&"

# decoded.txt is appended to across slots; cap its size so a long-lived
# channel's decode file doesn't grow without bound.  Generous — MSK144
# decodes are rare (meteor pings), so this is hit only on very active
# bands over many days.
MAX_DECODED_TXT_BYTES = 200_000


# arch (platform.machine()) → bundled jt9 binary basename.  v27 where we
# have it; arm32 only ships v26.  Mirror wspr-recorder's table so the two
# clients resolve the same binaries on the same hosts.
_JT9_BY_ARCH = {
    "x86_64":  "jt9-x86-v27",
    "amd64":   "jt9-x86-v27",
    "aarch64": "jt9-arm64-v27",
    "arm64":   "jt9-arm64-v27",
    "armv7l":  "jt9-arm32-v26",
    "armv6l":  "jt9-arm32-v26",
}

# bin/decoders sits at the repo root: this file is
# src/meteor_scatter/core/decoder.py → parents[3] == repo root.
_REPO_DECODER_DIR = Path(__file__).resolve().parents[3] / "bin" / "decoders"


def resolve_jt9_binary(explicit: str = "") -> str:
    """Resolve the jt9 binary path.

    Precedence: an explicit config override (``paths.decoder_jt9`` /
    ``paths.decoder``) wins if it exists; otherwise the arch-specific
    bundled binary under ``bin/decoders/``; otherwise a bare ``jt9`` on
    PATH.  Returns the resolved path string (may be the bare name if
    nothing was found — the caller surfaces the launch failure).
    """
    explicit = (explicit or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file() or shutil.which(explicit):
            return explicit
        logger.warning(
            "decoder override %r not found — falling back to bundled/"
            "PATH jt9 resolution", explicit,
        )

    arch = platform.machine().lower()
    name = _JT9_BY_ARCH.get(arch)
    if name:
        bundled = _REPO_DECODER_DIR / name
        if bundled.is_file():
            return str(bundled)
        logger.warning(
            "bundled jt9 for arch %s expected at %s but missing — "
            "falling back to PATH", arch, bundled,
        )
    else:
        logger.warning(
            "no bundled jt9 mapping for arch %r — falling back to PATH jt9",
            arch,
        )

    return shutil.which("jt9") or "jt9"


def build_jt9_cmd(jt9_path: str, workdir: Path, wav_path: Path) -> list[str]:
    """Build the ``jt9 --msk144`` argv for one slot WAV.

    ``-a workdir`` is jt9's writeable data dir (decoded.txt, wisdom,
    timer.out land there); run the process with ``cwd=workdir`` and
    pre-touch ``plotspec``/``decdata`` sentinels (jt9 opens them).

    ``-Y`` makes jt9 emit unresolved compound-callsign hashes as the
    numeric ``<NNNNNNN>`` form (22-bit) instead of the opaque ``<...>``.
    The shared callhash table (see ``ch_tailer`` → ``callhash.parse_message``)
    resolves those back to plaintext from accumulated ``<call>``
    announcements — the same mechanism wspr-recorder uses for FST4W.
    Without it, a hashed call the decoder couldn't resolve in-slot would
    surface as ``<...>`` and the spot would lose its call.
    """
    return [
        jt9_path,
        "-Y",
        "--msk144",
        "-p", str(MSK144_TR_PERIOD_SEC),
        "-f", str(MSK144_AUDIO_FREQ_HZ),
        "-a", str(workdir),
        str(wav_path),
    ]


def ensure_workdir(workdir: Path) -> None:
    """Create the jt9 data dir and the sentinel files it expects."""
    workdir.mkdir(parents=True, exist_ok=True)
    for sentinel in ("plotspec", "decdata"):
        try:
            (workdir / sentinel).touch(exist_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.debug("could not touch jt9 sentinel %s: %s", sentinel, exc)


def parse_decoded_txt_line(line: str) -> Optional[dict]:
    """Parse one raw ``decoded.txt`` line from a jt9 MSK144 run.

    jt9 writes a fixed-column line per decode.  The leading fields are
    ``<time> <snr> <dt> <audio_freq>`` followed by a one-character mode/
    sync indicator and then the freeform WSJT-X message.  Confirmed
    layout against real decodes is a Phase-2 live-validation item; this
    parser is deliberately tolerant of the time-column width (HHMM vs
    HHMMSS) and of the sync char being its own token or absent.

    Returns ``{snr, dt, audio_freq_hz, sync, message}`` or ``None`` when
    the line is the ``<DecodeFinished>`` sentinel / blank / unparseable.
    The authoritative UTC is supplied by the caller from the slot anchor
    (NOT from jt9's time column), so the time token is not returned.
    """
    s = line.strip()
    if not s or s.startswith("<DecodeFinished>") or s.startswith("<"):
        return None
    parts = s.split()
    if len(parts) < 5:
        return None
    try:
        snr = int(round(float(parts[1])))
        dt = float(parts[2])
        audio_freq = int(round(float(parts[3].replace(",", ""))))
    except (ValueError, IndexError):
        return None

    # parts[4] is the sync/mode indicator when it's a single non-alnum
    # char (e.g. "&"); otherwise the message starts at parts[4].
    idx = 4
    sync = ""
    if len(parts[4]) == 1 and not parts[4].isalnum():
        sync = parts[4]
        idx = 5
    message = " ".join(parts[idx:]).strip()
    if not message:
        return None
    return {
        "snr": snr,
        "dt": dt,
        "audio_freq_hz": audio_freq,
        "sync": sync or MSK144_SYNC_CHAR,
        "message": message,
    }


def normalize_log_line(decode: dict, slot_start, dial_freq_hz: int) -> str:
    """Render a parsed jt9 decode as a normalized per-mode-log line.

    Output shape (consumed by ``ch_tailer.parse_jt9_msk144_line``)::

        YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>

    * Date+time come from ``slot_start`` (a ``time.struct_time`` in UTC),
      the RTP-anchored slot boundary — authoritative, unlike jt9's own
      time column which it derives from the WAV filename.
    * ``abs_freq_hz`` = the channel dial frequency + jt9's audio offset,
      i.e. the true RF frequency of the decoded signal.
    * ``&`` marks this as an MSK144 decode so the tailer routes it to the
      MSK144 parser (vs the legacy ``~`` decode_ft8 path).
    """
    import time as _time
    ts = _time.strftime("%Y/%m/%d %H:%M:%S", slot_start)
    abs_freq = int(dial_freq_hz) + int(decode["audio_freq_hz"])
    return (
        f"{ts} {decode['snr']} {decode['dt']:+.2f} {abs_freq} "
        f"{MSK144_SYNC_CHAR} {decode['message']}\n"
    )
