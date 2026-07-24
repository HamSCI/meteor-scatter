"""TOML config loader and defaults for meteor-scatter."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("/etc/meteor-scatter/meteor-scatter-config.toml")
PER_INSTANCE_CONFIG_DIR = Path("/etc/meteor-scatter")


def resolve_config_path(
    instance: Optional[str] = None,
    explicit_path: Optional[Path] = None,
) -> Path:
    """Resolve which config file to load for this invocation.

    Resolution order (most → least specific):
      1. `explicit_path` (operator passed --config) — always wins.
      2. `$METEOR_SCATTER_CONFIG` env var — explicit override.
      3. `/etc/meteor-scatter/<instance>.toml` when `instance` is given
         and the file exists — the per-instance v0.8 world
         (sigmond's MULTI-INSTANCE-ARCHITECTURE.md §4).
      4. `/etc/meteor-scatter/meteor-scatter-config.toml` (legacy shared)
         — emits a DeprecationWarning when `instance` was given but
         the per-instance file does not exist (operator hasn't run
         `sudo smd instance migrate` yet).
      5. `/etc/meteor-scatter/meteor-scatter-config.toml` (legacy shared)
         silently when no instance was given (pre-instance world).
    """
    if explicit_path is not None:
        return Path(explicit_path)
    env_override = os.environ.get("METEOR_SCATTER_CONFIG")
    if env_override:
        return Path(env_override)
    if instance:
        per_instance = PER_INSTANCE_CONFIG_DIR / f"{instance}.toml"
        if per_instance.exists():
            return per_instance
        warnings.warn(
            f"per-instance config {per_instance} not found; falling "
            f"back to legacy shared config {DEFAULT_CONFIG_PATH}. "
            f"Migrate this host with `sudo smd instance migrate` "
            f"(MULTI-INSTANCE-ARCHITECTURE.md §6) — the legacy path "
            f"will be removed after the deprecation window.",
            DeprecationWarning,
            stacklevel=2,
        )
    return DEFAULT_CONFIG_PATH


def extract_reporter_id(config: dict) -> Optional[str]:
    """Read the reporter ID from the per-instance `[instance]` block.

    Returns None when the config has no `[instance]` block (legacy
    shared-config world).  Callers should fall back to a derived
    identifier (e.g. the radiod_id) when None is returned, so every
    spot row still carries a meaningful identifier during the
    deprecation window.
    """
    inst = config.get("instance")
    if not isinstance(inst, dict):
        return None
    rid = inst.get("reporter_id")
    if not isinstance(rid, str) or not rid:
        return None
    return rid

DEFAULTS: dict[str, Any] = {
    "paths": {
        "spool_dir": "/var/lib/meteor-scatter",
        "log_dir": "/var/log/meteor-scatter",
        # Empty by default — jt9 is resolved from /usr/local/bin at runtime
        # (sigmond's from-source wsjtx-decoders build; see
        # core.decoder.resolve_jt9_binary).  Set paths.decoder_jt9 to override.
        "decoder": "",
        "keep_wav": False,
    },
    "processing": {
        # radiod LIFETIME tag (ka9q-python ≥3.13.0, ka9q-radio ≥0f8b622).
        # Channels self-destruct after this many radiod main-loop frames
        # (~50 Hz at the default 20 ms blocktime, so 6000 ≈ 2 min).  The
        # recorder refreshes lifetime every (frames / 4) seconds while
        # running, so a crashed/killed recorder leaves no residual
        # channels on radiod within ~2 min.
        # 0 = infinite (no LIFETIME tag, no keep-alive — radiod owns the
        # channel for its full template default).  Use only when you
        # truly want a channel to outlive the recorder.
        "radiod_lifetime_frames": 6000,
    },
}

# MSK144 T/R sequence length (seconds).  This MUST match the T/R period the
# transmitting WSJT-X stations use, or the recorded slots won't frame their
# transmissions cleanly (a mismatch still decodes — MSK144 frames are self-
# contained — but the same TX shows up in adjacent slots with a wandering dt).
# A stock WSJT-X install defaults MSK144 to 30 s, so that is our default;
# WSJT-X also offers 5/10/15 s.  Overridable per radiod via the
# ``[radiod.msk144] tr_period_sec`` key.  We record one T/R-aligned slot per
# period and decode it once with ``jt9 --msk144 -p <tr_period_sec>``.
MSK144_CADENCE_SEC = 30.0
# Single decode mode for this client.  Kept as a tuple so the per-mode loops
# inherited from the FT4/FT8 skeleton iterate cleanly over just this one.
MODES = ("msk144",)
DEFAULT_SAMPLE_RATE = 12000
DEFAULT_PRESET = "usb"
DEFAULT_ENCODING = "s16be"


def load_config(path: Path | None = None) -> dict:
    """Load and merge config with defaults."""
    config_path = path or Path(
        os.environ.get("METEOR_SCATTER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    raw.setdefault("paths", {})
    for key, val in DEFAULTS["paths"].items():
        raw["paths"].setdefault(key, val)

    raw.setdefault("processing", {})
    for key, val in DEFAULTS["processing"].items():
        raw["processing"].setdefault(key, val)

    lifetime = raw["processing"]["radiod_lifetime_frames"]
    if not isinstance(lifetime, int) or lifetime < 0:
        raise ValueError(
            f"processing.radiod_lifetime_frames must be a non-negative int "
            f"(frames; ~50 Hz at default blocktime); got {lifetime!r}"
        )

    return raw


def resolve_radiod_block(config: dict, radiod_id: str | None) -> dict:
    """Find the [[radiod]] block matching radiod_id.

    Match on the canonical ``status`` field (mDNS multicast name) per
    RADIOD-IDENTIFICATION.md §3.1.  Phase 6 cutover (this release)
    removed acceptance of the legacy ``id`` field; operators still
    using legacy configs must run ``sudo smd radiod migrate --yes``
    before restarting daemons.

    If radiod_id is None, the config must contain exactly one [[radiod]].
    """
    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]

    if not radiod_blocks:
        raise ValueError("Config contains no [[radiod]] blocks")

    if radiod_id is None:
        if len(radiod_blocks) != 1:
            raise ValueError(
                f"--radiod-id required: config has {len(radiod_blocks)} "
                f"[[radiod]] blocks"
            )
        return radiod_blocks[0]

    for block in radiod_blocks:
        if block.get("status") == radiod_id:
            return block

    available = [b.get("status", "<unnamed>") for b in radiod_blocks]
    raise ValueError(
        f"No [[radiod]] block with status={radiod_id!r}. "
        f"Available: {', '.join(available)}.  "
        "If you see legacy `id` fields in the config, run "
        "`sudo smd radiod migrate --yes` to rewrite them per "
        "RADIOD-IDENTIFICATION.md §3.1."
    )


def get_freqs(radiod_block: dict, mode: str = "msk144") -> list[int]:
    """Extract frequency list for a mode (this client uses only 'msk144')."""
    mode_block = radiod_block.get(mode, {})
    return list(mode_block.get("freqs_hz", []))


def get_mode_params(radiod_block: dict, mode: str) -> dict:
    """Extract sample_rate, preset, encoding, tr_period_sec for a mode."""
    mode_block = radiod_block.get(mode, {})
    return {
        "sample_rate": int(mode_block.get("sample_rate", DEFAULT_SAMPLE_RATE)),
        "preset": mode_block.get("preset", DEFAULT_PRESET),
        "encoding": mode_block.get("encoding", DEFAULT_ENCODING),
        "tr_period_sec": float(
            mode_block.get("tr_period_sec", MSK144_CADENCE_SEC)
        ),
    }


# Unconfigured-radiod sentinel — sigmond bring-up seeds a fresh recorder
# config with this placeholder in place of the radiod mDNS status name
# (mirrors sigmond.harmonize._RADIOD_STATUS_PLACEHOLDER).  It can never
# resolve, so a daemon started against it would crash-loop forever; the
# daemon fails fast (EX_CONFIG 78) and `validate` flags it instead.
RADIOD_STATUS_PLACEHOLDER = "<configure-via-config-init>"


def is_placeholder_status(status) -> bool:
    """True when a radiod `status` value is still the unconfigured sentinel."""
    return (status or "").strip() == RADIOD_STATUS_PLACEHOLDER


def resolve_radiod_status(radiod_block: dict) -> str:
    """Resolve the radiod mDNS control/status multicast name.

    Reads the canonical ``status`` field per
    RADIOD-IDENTIFICATION.md §3.1.  Phase 6 cutover (this release)
    removed the legacy paths (``RADIOD_<ID>_STATUS`` env override
    and ``radiod_status`` field) — operators with legacy configs
    must run ``sudo smd radiod migrate --yes``.
    """
    status = radiod_block.get("status")
    if not status:
        raise ValueError(
            "[[radiod]] block has no `status` field.  Run "
            "`sudo smd radiod migrate --yes` if this config still "
            "uses the legacy `radiod_status` field, or run "
            "`meteor-scatter config init` for a fresh config."
        )
    return status


def derive_source_key(radiod_block: dict) -> str:
    """Canonical source identifier for a radiod block.

    Returns ``radiod:<status_address>`` — matches
    ``sigmond.sources.SourceKey`` string form and wspr-recorder's
    ``SourceConfig.key`` so a spot's ``rx_source`` tag is comparable
    across clients.
    """
    return f"radiod:{resolve_radiod_status(radiod_block)}"


def ensure_sources(config: dict) -> list[dict]:
    """Normalise ``[[radiod]]`` blocks into a list of source descriptors.

    Each entry has:
      ``key``               — ``radiod:<status_address>`` (matches
                              ``derive_source_key``)
      ``radiod_id``         — the mDNS status name (canonical id per
                              RADIOD-IDENTIFICATION.md §3.1)
      ``status_address``    — same value, kept for callers that read
                              the field by that name
      ``radiod_block``      — the original block (freqs, lifetime, etc.)
    """
    blocks = config.get("radiod", [])
    if isinstance(blocks, dict):
        blocks = [blocks]

    sources: list[dict] = []
    for block in blocks:
        try:
            status = resolve_radiod_status(block)
        except ValueError:
            # Skip blocks missing the `status` field — same shape
            # ``resolve_radiod_block`` would reject; callers should
            # not assume every entry resolves.
            continue
        sources.append({
            "key": f"radiod:{status}",
            "radiod_id": status,
            "status_address": status,
            "radiod_block": block,
        })
    return sources
