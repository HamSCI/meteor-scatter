"""Interactive `config init` and `config edit` for meteor-scatter.

Three operator-facing paths:

1. **Interactive whiptail wizard (default)** — when stdout is a TTY
   and ``whiptail`` is available, exec ``scripts/config-wizard.sh``.
   The wizard talks to this module via ``config show --json
   --defaults`` and ``config apply --json -``; same UI shape as
   the mag-recorder pilot (sigmond-integration HEAD 5b02db1, see
   that repo's scripts/config-wizard.sh).

2. **Legacy stdin prompts** — original ``_prompt()``-based flow.
   Runs when whiptail isn't installed, or when the operator passes
   ``--non-interactive``, or when stdout isn't a TTY.  Implements
   CONTRACT-v0.5 §14: sigmond invokes via ``smd config init|edit
   meteor-scatter [<instance>]``, passing ``STATION_CALL``,
   ``STATION_GRID``, ``SIGMOND_INSTANCE``, and
   ``SIGMOND_RADIOD_STATUS`` as advisory defaults.

3. **--non-interactive** — same as the legacy fallback, but never
   prompts (env-bag values land if set, otherwise placeholders
   stay).  Used by sigmond's scripted first-run interview.

Standalone usage works in all modes (env vars unset → empty defaults).

[[radiod]] arrays-of-tables and per-band ``freqs_hz`` lists are not
modifiable through ``config apply``.  Whiptail can't express them
naturally, and ``tomllib`` can't preserve TOML comments / formatting
across a round-trip.  The wizard offers an "Edit raw TOML in
\$EDITOR" menu item for the rare cases that need them.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from .config import DEFAULT_CONFIG_PATH, DEFAULTS, load_config


# ---------------------------------------------------------------------------
# Wizard / help-toml paths.  Resolved at import time so the CLI's
# subprocess.call hands the correct paths to the shell wizard.
# ---------------------------------------------------------------------------

_WIZARD_PATH = Path(__file__).resolve().parent.parent.parent \
    / "scripts" / "config-wizard.sh"

_HELP_TOML_PATH = Path(__file__).resolve().parent.parent.parent \
    / "config" / "help.toml"


# Repo-relative location of the template (works for editable installs and
# packaged installs alike).
def _find_template() -> Optional[Path]:
    candidates = [
        Path(__file__).resolve().parent.parent.parent
            / "config" / "meteor-scatter-config.toml.template",
        Path("/opt/git/sigmond/meteor-scatter/config/meteor-scatter-config.toml.template"),
        Path("/usr/local/share/meteor-scatter/meteor-scatter-config.toml.template"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Public entry points (called from cli.py)
# ---------------------------------------------------------------------------

def cmd_config_init(args) -> int:
    """Dispatch `config init` to whiptail wizard or legacy stdin prompts."""
    if not getattr(args, "non_interactive", False) and _wizard_available(args):
        # Make sure something exists for the wizard to load -- if the
        # operator never ran the legacy template render, write the
        # template first so `config show --defaults` has a real file
        # to start from.  Idempotent: respects --reconfig.
        target = _resolve_target(args)
        if not target.exists() or getattr(args, "reconfig", False):
            rv = _legacy_config_init(args)
            if rv != 0:
                return rv
        return _exec_wizard(args, "init")
    return _legacy_config_init(args)


def cmd_config_edit(args) -> int:
    """Dispatch `config edit` to whiptail wizard or legacy stdin prompts."""
    target = _resolve_target(args)
    if not target.exists():
        _err(f"{target} does not exist.  Run `meteor-scatter config init` first.")
        return 1
    if not getattr(args, "non_interactive", False) and _wizard_available(args):
        return _exec_wizard(args, "edit")
    return _legacy_config_edit(args)


def _enable_instance(radiod_id: str) -> None:
    """Enable the systemd instance for this radiod id so sigmond's lifecycle
    (and a plain `systemctl start`) bring it up.  Best-effort: a missing
    systemctl or lack of privilege is non-fatal -- the config is still written."""
    import shutil
    import subprocess
    if not radiod_id:
        return
    sctl = shutil.which("systemctl")
    if not sctl:
        return
    unit = f"meteor-scatter@{radiod_id}.service"
    try:
        r = subprocess.run([sctl, "enable", unit], capture_output=True, text=True)
    except OSError:
        return
    if r.returncode == 0:
        _ok(f"enabled {unit}")
    else:
        _info(f"(could not enable {unit} -- enable manually: "
              f"sudo systemctl enable {unit})")


def _legacy_config_init(args) -> int:
    target = _resolve_target(args)
    if target.exists() and not getattr(args, "reconfig", False):
        _err(f"{target} already exists.  Pass --reconfig to overwrite, or "
             f"run `meteor-scatter config edit` instead.")
        return 1

    template = _find_template()
    if template is None:
        _err("meteor-scatter template not found; reinstall or pass --template")
        return 1

    # Read template, then patch with operator/env values.
    body = template.read_text()
    values = _collect_init_values(args)
    body = _apply_init_substitutions(body, values)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _ok(f"wrote {target}")
    _enable_instance(values["radiod_id"])
    _info(f"reporter: {values['callsign']}    grid: {values['grid']}")
    _info(f"radiod:   id={values['radiod_id']}  status={values['radiod_status']}")
    _info("")
    _info("Next steps:")
    _info(f"  1. Review the [radiod.msk144] freqs_hz array in {target}")
    _info(f"  2. Validate: meteor-scatter validate --json")
    _info(f"  3. Start:    sudo systemctl start "
          f"meteor-scatter@{values['radiod_id']}.service  (instance already enabled)")
    return 0


def _legacy_config_edit(args) -> int:
    target = _resolve_target(args)
    if not target.exists():
        _err(f"{target} does not exist.  Run `meteor-scatter config init` first.")
        return 1

    try:
        with open(target, "rb") as f:
            current = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _err(f"failed to read {target}: {e}")
        return 1

    # In edit mode, the *current config* is the primary default; the env bag
    # only fills in values where the config is empty (CONTRACT-v0.5 §14, edit
    # flow).
    cur_call = (current.get("station") or {}).get("callsign", "")
    cur_grid = (current.get("station") or {}).get("grid_square", "")
    blocks = _radiod_blocks(current)
    block, block_index = _select_radiod_block(blocks, args)
    if block is None:
        return 1
    cur_id     = block.get("id", "")
    cur_status = block.get("radiod_status", "")

    if getattr(args, "non_interactive", False):
        # Display only.
        _info(f"station.callsign      = {cur_call}")
        _info(f"station.grid_square   = {cur_grid}")
        _info(f"radiod[{block_index}].id           = {cur_id}")
        _info(f"radiod[{block_index}].radiod_status = {cur_status}")
        return 0

    new_call = _prompt(
        "Callsign",
        cur_call or _default_reporter_callsign(
            os.environ.get("STATION_CALL", "")))
    new_grid = _prompt("Grid square",
                       cur_grid or os.environ.get("STATION_GRID", ""))
    new_id = _prompt("Radiod id",
                     cur_id or os.environ.get("SIGMOND_INSTANCE", ""))
    new_status = _prompt("Radiod status DNS",
                         cur_status or
                         os.environ.get("SIGMOND_RADIOD_STATUS", ""))

    body = target.read_text()
    body = _replace_station_field(body, "callsign",    new_call)
    body = _replace_station_field(body, "grid_square", new_grid)
    body = _replace_radiod_field(body, block_index, "id",            new_id)
    body = _replace_radiod_field(body, block_index, "radiod_status", new_status)

    if body == target.read_text():
        _info("no changes")
        return 0

    target.write_text(body)
    _ok(f"updated {target}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target(args) -> Path:
    return Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)


def _discover_radiods(timeout: float = 5.0) -> list[dict]:
    """Return discovered radiods or [] on failure (avahi missing, etc.).

    Lazy-imports ka9q.discovery so the configurator still works when
    ka9q-python isn't installed yet (e.g. mid-bootstrap).  Each entry
    is {"name": str, "address": str} — `address` is the mDNS
    multicast control/status name (e.g. "bee1-status.local") which
    becomes the value of the new `[[radiod]] status` field per
    RADIOD-IDENTIFICATION.md §3.1.
    """
    try:
        from ka9q.discovery import discover_radiod_services
        return discover_radiod_services(timeout=timeout) or []
    except Exception:
        return []


def _pick_radiod_status_from_discovery(
    discovered: list[dict], env_status: str, instance_hint: str,
) -> str:
    """Interactive discovery flow per RADIOD-IDENTIFICATION.md §4.

    Returns the chosen multicast status name (e.g. "bee1-status.local").

    Cases:
      0 discovered → warn + fall back to manual entry (env or prompt)
      1 discovered → confirm (default Y)
      ≥2 discovered → menu chooser; operator picks one
    """
    # The host's own radiod (passed by sigmond as SIGMOND_RADIOD_STATUS) may
    # not be broadcasting yet: during `smd bringup` the per-client config
    # interviews run BEFORE radiod is started, so mDNS can't see it.  Inject it
    # as the preferred (first/default) entry so the operator can select their
    # own configured-but-not-yet-running radiod instead of being forced to pick
    # an unrelated one off the LAN.
    discovered = list(discovered)
    if env_status:
        # Put the host's own radiod (SIGMOND_RADIOD_STATUS) first so it is the
        # default pick — whether mDNS already discovered it (radiod running) or
        # not yet (still starting during bring-up, in which case we inject it).
        local = next(
            (d for d in discovered if d.get("hostname") == env_status), None)
        if local is not None:
            discovered.remove(local)
            discovered.insert(0, local)
        else:
            discovered.insert(0, {
                "hostname": env_status,
                "name": "this host — configured, starts with bring-up",
            })

    if not discovered:
        print("\033[33m⚠\033[0m  No radiod instances broadcasting on the "
              "local network.")
        _info("Install + start radiod before continuing:")
        _info("  sudo smd install ka9q-radio")
        _info("Continuing with manual entry — the daemon will refuse to "
              "start if the multicast name is unreachable.")
        default = env_status or (
            f"{instance_hint}-status.local" if instance_hint else "")
        return _prompt(
            "Radiod status DNS (manual entry)", default, required=True)

    # `hostname` is the mDNS multicast control/status name (per
    # ka9q-python's enhanced discover_radiod_services).  This is the
    # canonical identifier per RADIOD-IDENTIFICATION.md §2.  We never
    # use the `address` (multicast IP) — it's discoverable but
    # unstable across radiod restarts.
    if len(discovered) == 1:
        only = discovered[0]
        _info(f"One radiod discovered: {only['hostname']!r} "
              f"(advertised name: {only['name']!r})")
        confirm = _prompt(
            f"Use {only['hostname']!r}? [Y/n]", "Y").strip().lower()
        if confirm in ("", "y", "yes"):
            return only["hostname"]
        # Operator declined the only choice — fall back to manual.
        return _prompt(
            "Radiod status DNS (manual entry)",
            env_status or only["hostname"], required=True)

    # Multi-radiod menu.
    _info("Multiple radiods discovered on the LAN:")
    for i, svc in enumerate(discovered, 1):
        _info(f"  [{i}] {svc['hostname']:<32} (advertised: {svc['name']!r})")
    while True:
        choice = _prompt(
            f"Pick a radiod [1-{len(discovered)}]", "1").strip()
        try:
            idx = int(choice) - 1
        except ValueError:
            print("\033[33m⚠\033[0m  Enter a number from the menu.")
            continue
        if 0 <= idx < len(discovered):
            return discovered[idx]["hostname"]
        print(f"\033[33m⚠\033[0m  Out of range; pick 1-{len(discovered)}.")


def _collect_init_values(args) -> dict:
    """Build the substitution dict for init.

    Env vars are defaults; ka9q-python discovery is consulted in
    interactive mode (RADIOD-IDENTIFICATION.md §4); a prompt fills in
    anything missing unless --non-interactive."""
    call = os.environ.get("STATION_CALL", "")
    grid = os.environ.get("STATION_GRID", "")
    instance = os.environ.get("SIGMOND_INSTANCE", "")
    env_status = os.environ.get("SIGMOND_RADIOD_STATUS", "")
    default_callsign = _default_reporter_callsign(call)

    if getattr(args, "non_interactive", False):
        # Non-interactive: env wins.  If env is empty and exactly one
        # radiod is discoverable on the LAN, take it; otherwise fall
        # back to the legacy placeholder shape so the rendered config
        # is syntactically valid (operator can re-run interactively).
        if env_status:
            radiod_status = env_status
        else:
            discovered = _discover_radiods()
            if len(discovered) == 1:
                radiod_status = discovered[0]["hostname"]
            else:
                radiod_status = (
                    f"{instance}-status.local"
                    if instance else "my-rx888-status.local"
                )
        return {
            "callsign":      default_callsign or "YOURCALL",
            "grid":          grid or "AA00aa",
            "radiod_id":     instance or "my-rx888",
            "radiod_status": radiod_status,
        }

    callsign = _prompt("Callsign", default_callsign, required=True)
    grid_square = _prompt("Grid square", grid, required=True)

    # RADIOD-IDENTIFICATION.md §4 — discovery-driven radiod selection.
    discovered = _discover_radiods()
    radiod_status = _pick_radiod_status_from_discovery(
        discovered, env_status, instance)

    # Legacy `id` local label.  Phase 6 cutover removes this from the
    # interactive flow entirely; for now it's still asked because
    # build_inventory uses it for env-var key derivation + log/spool
    # file naming during the deprecation window.  Default derived from
    # status when no env hint is present.
    radiod_id_default = instance or _derive_label_from_status(radiod_status)
    radiod_id = _prompt("Radiod id (local label — legacy, will be retired)",
                        radiod_id_default, required=True)
    return {
        "callsign":      callsign,
        "grid":          grid_square,
        "radiod_id":     radiod_id,
        "radiod_status": radiod_status,
    }


def _derive_label_from_status(status: str) -> str:
    """Strip common mDNS suffixes to make a default local label.
    ``bee1-status.local`` → ``bee1``."""
    base = (status or "").strip()
    for suffix in ("-status.local", ".local"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base or "default"


def _default_reporter_callsign(call: str) -> str:
    """Compose the reporter callsign default from the bare callsign per
    CONTRACT-v0.5 §14.6:

    - single radiod (SIGMOND_RADIOD_COUNT == 1 or unset): bare callsign.
    - multi-radiod: AC0G/B<n> where n is SIGMOND_RADIOD_INDEX, falling
      back to 1 when not set.

    Returns "" when no callsign is known.
    """
    if not call:
        return ""
    try:
        count = int(os.environ.get("SIGMOND_RADIOD_COUNT", "1") or "1")
    except ValueError:
        count = 1
    if count <= 1:
        return call
    try:
        index = int(os.environ.get("SIGMOND_RADIOD_INDEX", "1") or "1")
    except ValueError:
        index = 1
    return f"{call}/B{index}"


def _apply_init_substitutions(body: str, values: dict) -> str:
    body = _replace_station_field(body, "callsign",    values["callsign"])
    body = _replace_station_field(body, "grid_square", values["grid"])
    # RADIOD-IDENTIFICATION.md §3.1 — the canonical field is `status`
    # (multicast mDNS name).  The legacy `id` and `radiod_status`
    # lines are commented out in the template; substitute the operator-
    # supplied multicast name into the active `status = ...` line.
    # Phase 4 will replace the env-var-driven defaults with
    # ka9q-python discovery.
    body = _replace_radiod_field(body, 0, "status", values["radiod_status"])
    return body


def _radiod_blocks(config: dict) -> list[dict]:
    blocks = config.get("radiod", [])
    if isinstance(blocks, dict):
        blocks = [blocks]
    return list(blocks)


def _select_radiod_block(blocks: list[dict], args) -> tuple:
    """Return (block, index) of the radiod block to edit.
    Picks: SIGMOND_INSTANCE if set, else the only block, else prompts."""
    if not blocks:
        _err("config has no [[radiod]] blocks")
        return None, -1

    target_id = os.environ.get("SIGMOND_INSTANCE", "") or \
                getattr(args, "radiod_id", None)

    if target_id:
        for i, b in enumerate(blocks):
            if b.get("id") == target_id:
                return b, i
        _err(f"no [[radiod]] block with id={target_id!r}; "
             f"available: {', '.join(b.get('id', '?') for b in blocks)}")
        return None, -1

    if len(blocks) == 1:
        return blocks[0], 0

    if getattr(args, "non_interactive", False):
        _err(f"multiple [[radiod]] blocks; specify with --radiod-id or "
             f"SIGMOND_INSTANCE")
        return None, -1

    print("\nMultiple [[radiod]] blocks present.  Pick one:")
    for i, b in enumerate(blocks, start=1):
        print(f"  {i}) id={b.get('id', '?')}  status={b.get('radiod_status', '?')}")
    while True:
        choice = input("Select [1-{}]: ".format(len(blocks))).strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(blocks):
                return blocks[idx], idx
        except ValueError:
            pass
        print("  invalid choice")


# ---------------------------------------------------------------------------
# Field substitution — line-oriented; preserves comments and surrounding TOML.
# ---------------------------------------------------------------------------

_STATION_PAT = re.compile(
    r'^(\s*{key}\s*=\s*)"[^"]*"(.*)$'
)


def _replace_station_field(body: str, key: str, value: str) -> str:
    """Replace `key = "..."` inside the [station] block."""
    pat = re.compile(
        r'^(\s*' + re.escape(key) + r'\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
    )
    in_station = False
    out_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            in_station = (stripped == "[station]")
        if in_station:
            line = pat.sub(rf'\g<1>"{value}"\g<2>', line)
        out_lines.append(line)
    return ''.join(out_lines)


def _replace_radiod_field(body: str, index: int, key: str, value: str) -> str:
    """Replace `key = "..."` inside the Nth top-level [[radiod]] block.
    Stops at the next [[radiod]] header or the next top-level table that
    isn't a sub-table of radiod."""
    pat = re.compile(
        r'^(\s*' + re.escape(key) + r'\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
    )
    out_lines: list[str] = []
    radiod_count = -1
    in_target = False
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "[[radiod]]":
            radiod_count += 1
            in_target = (radiod_count == index)
        elif (stripped.startswith('[[') and stripped.endswith(']]')
              and stripped != "[[radiod]]"):
            in_target = False
        elif (stripped.startswith('[') and not stripped.startswith('[[')
              and not stripped.startswith('[radiod.')):
            # A top-level [section] that isn't [radiod.<sub>] ends our scope.
            in_target = False
        if in_target:
            line = pat.sub(rf'\g<1>"{value}"\g<2>', line)
        out_lines.append(line)
    return ''.join(out_lines)


# ---------------------------------------------------------------------------
# Prompts (small, dependency-free)
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str, *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            raw = ""
        result = raw or default
        if result or not required:
            return result
        print("  This field is required.")


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:
    print(f"\033[32m✓\033[0m {msg}")


def _info(msg: str) -> None:
    print(f"  {msg}")


def _err(msg: str) -> None:
    print(f"\033[31m✗\033[0m {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Whiptail wizard dispatcher.
#
# Delegates to sigmond.wizard_dispatch when sigmond is importable (the
# canonical shared lib -- mag-recorder + wspr-recorder also adopt or
# will adopt the same lib); falls back to the original local
# implementation when sigmond isn't installed (older deploys,
# standalone-host operators who skipped `pip install -e
# /opt/git/sigmond/sigmond`).  Local fallback keeps the previous
# behaviour exactly so meteor-scatter still works standalone.
#
# Same shape as mag-recorder's adoption (mag-recorder commit 52190e7).
# ---------------------------------------------------------------------------

try:
    import sigmond.wizard_dispatch as _sigmond_wd
    # Pin the API contract version.  A future incompatible bump in
    # sigmond should fail loudly here rather than silently dispatch
    # through the wrong call signature.
    assert _sigmond_wd.SIGMOND_WIZARD_DISPATCH_API == "1", (
        f"sigmond.wizard_dispatch API "
        f"{_sigmond_wd.SIGMOND_WIZARD_DISPATCH_API!r} != '1' "
        f"(expected by meteor-scatter)"
    )
    # Locate the shell-side helpers next to the Python module so the
    # wizard script can `source` them regardless of where sigmond
    # ended up on this host.
    _SIGMOND_WIZARD_LIB_SH: Optional[Path] = (
        Path(_sigmond_wd.__file__).resolve().parent / "wizard_dispatch.sh"
    )
    if not _SIGMOND_WIZARD_LIB_SH.is_file():
        _SIGMOND_WIZARD_LIB_SH = None
except (ImportError, AssertionError):
    _sigmond_wd = None
    _SIGMOND_WIZARD_LIB_SH = None


def _wizard_available(args=None) -> bool:
    """True iff we should exec the shell wizard for this run.

    When sigmond is importable, defers to sigmond.wizard_dispatch.
    is_wizard_available(args, _WIZARD_PATH) so all three clients
    (mag-recorder, meteor-scatter, wspr-recorder) honour the same gate.

    When sigmond isn't installed, falls back to the original
    standalone check.  Behaviour bit-for-bit identical to the
    pre-extraction local helper.
    """
    if _sigmond_wd is not None:
        # Callers always have args in scope today; defensive default
        # for any future caller that might forget to pass it.
        if args is None:
            import argparse as _argparse
            args = _argparse.Namespace(non_interactive=False)
        return _sigmond_wd.is_wizard_available(args, _WIZARD_PATH)

    # Local fallback (verbatim from pre-extraction).
    if not _WIZARD_PATH.is_file():
        return False
    if not os.access(_WIZARD_PATH, os.X_OK):
        return False
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False
    return shutil.which("whiptail") is not None


def _exec_wizard(args, mode: str) -> int:
    """Hand off to scripts/config-wizard.sh, preserving --config."""
    extra_env: dict = {
        # Tell the wizard where the help sidecar is so it doesn't have
        # to guess (matters when meteor-scatter is installed editable
        # from /opt/git/sigmond/meteor-scatter and run from /usr/local/bin).
        "METEOR_SCATTER_HELP_TOML": str(_HELP_TOML_PATH),
        # The binary path: wizard shells out to `meteor-scatter config
        # show/apply` and needs to call the same binary the caller
        # used (so a non-default --config keeps working).
        "METEOR_SCATTER_CLI": sys.argv[0],
    }
    extra_args = [mode]
    config_arg = getattr(args, "config", None)
    if config_arg:
        extra_args += ["--config", str(config_arg)]

    if _sigmond_wd is not None:
        # Hand the wizard script the path to sigmond's shell helpers
        # so it can `source` the shared preflight + loggers without
        # hard-coding /opt/git/sigmond/...  Falls through to the
        # script's own :- default when this env var isn't set
        # (direct-invocation safety net).
        if _SIGMOND_WIZARD_LIB_SH is not None:
            extra_env["SIGMOND_WIZARD_LIB_SH"] = str(_SIGMOND_WIZARD_LIB_SH)
        # parse=None: meteor-scatter's wizard pipes JSON directly into
        # `meteor-scatter config apply` itself; we don't parse stdout.
        # Default interactive=True (sigmond.wizard_dispatch 1.x): child
        # inherits the parent's TTY so whiptail can render dialogs.
        result = _sigmond_wd.exec_wizard(
            _WIZARD_PATH,
            extra_env=extra_env,
            parse=None,
            extra_args=extra_args,
        )
        if result.error:
            _err(f"wizard exec failed: {result.error}")
            return 1
        return result.returncode

    # Local fallback (sigmond not importable).
    cmd = [str(_WIZARD_PATH)] + extra_args
    env = os.environ.copy()
    env.update(extra_env)
    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError as exc:
        _err(f"wizard exec failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# config show --json [--defaults] / config apply --json -
# ---------------------------------------------------------------------------
#
# These are the only two surfaces the whiptail wizard touches.  Schema
# knowledge stays in this module + config.DEFAULTS.  Same pattern
# mag-recorder's configurator.py uses (sigmond-integration HEAD
# 5b02db1) -- if the per-client wizard pattern proves out across N
# clients, the show/apply/_serialize_toml machinery is a candidate
# for extraction into a sigmond-provided library.

def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return a new dict: ``overlay`` keys win, recursing into nested dicts.

    [[radiod]] is a TOML array-of-tables (list of dicts on the Python
    side), not a nested dict, so it's never merged -- overlay wins
    outright when present.  The wizard never sets it, so in practice
    the existing [[radiod]] blocks in the file pass through untouched.
    """
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def cmd_config_show(args) -> int:
    """Emit the current config as JSON on stdout.

    With ``--defaults`` the output is ``DEFAULTS`` deep-merged with
    whatever's in the TOML file, so the wizard sees every key with
    a sensible value even on a freshly-installed host.  Without
    ``--defaults`` only the keys actually present in the file appear.

    Note: meteor-scatter's DEFAULTS only covers [paths] and [processing];
    [station] and [[radiod]] aren't defaulted (operators must fill them
    in).  --defaults still includes the file's [station] / [[radiod]]
    verbatim.
    """
    config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    if getattr(args, "defaults", False):
        if config_path.is_file():
            try:
                effective = load_config(config_path)
            except (FileNotFoundError, ValueError):
                effective = copy.deepcopy(DEFAULTS)
        else:
            effective = copy.deepcopy(DEFAULTS)
        out = effective
    else:
        if not config_path.is_file():
            out = {}
        else:
            with open(config_path, "rb") as f:
                out = tomllib.load(f)
    json.dump(out, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


# Sections the apply path is allowed to write.
#
# [station] / [paths] / [processing] / [timing] are scalar-only tables,
# easy to type-check against DEFAULTS, and naturally edited via
# whiptail inputboxes.
#
# [[radiod]] is a TOML array-of-tables.  The wizard's "Radiod" section
# walks ``id`` + ``radiod_status`` inline for each block (or adds a
# new one), but per-band ``freqs_hz`` lists are too large for a
# whiptail dialog -- those still escape to $EDITOR via the menu's
# "Edit raw TOML" option.  Merge semantics: the operator's full
# block list REPLACES the file's list (overlay-wins), since deep-merge
# doesn't compose for arrays-of-tables.
_APPLY_ALLOWED_SECTIONS = {"station", "paths", "processing", "timing", "radiod"}


def cmd_config_apply(args) -> int:
    """Read a JSON dict on stdin, validate, atomically write the TOML.

    For meteor-scatter the apply surface is intentionally smaller than
    the full schema: only [station], [paths], [processing] can be set
    here.  [[radiod]] arrays-of-tables are not modifiable through
    this path -- see _APPLY_ALLOWED_SECTIONS for the rationale.

    Validation:
      - Only sections in _APPLY_ALLOWED_SECTIONS may appear.
      - Per-key types are checked against DEFAULTS where defaults
        exist; [station] is type-loose (everything's a string).
      - The merged config must pass load_config()'s validators
        (radiod_lifetime_frames range, etc.) before we write.

    On success the file is rewritten atomically via .part + rename.
    Comments and formatting are NOT preserved -- the operator who
    wants comments back can re-run ``config init --reconfig``.
    """
    config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"config apply: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print(f"config apply: top-level JSON must be an object, got {type(payload).__name__}",
              file=sys.stderr)
        return 2

    unknown = set(payload.keys()) - _APPLY_ALLOWED_SECTIONS
    if unknown:
        print(f"config apply: section(s) not writable via apply: {sorted(unknown)} "
              f"(allowed: {sorted(_APPLY_ALLOWED_SECTIONS)})",
              file=sys.stderr)
        return 2

    for section, fields in payload.items():
        # [[radiod]] arrives as a JSON list (the operator's full block
        # list; overlay-wins replaces the file's list since deep-merge
        # doesn't compose for arrays-of-tables).
        if section == "radiod":
            if not isinstance(fields, list):
                print(f"config apply: [[radiod]] must be a list, got {type(fields).__name__}",
                      file=sys.stderr)
                return 2
            for i, block in enumerate(fields):
                if not isinstance(block, dict):
                    print(f"config apply: [[radiod]][{i}] must be a table, got {type(block).__name__}",
                          file=sys.stderr)
                    return 2
                # RADIOD-IDENTIFICATION.md §3.1 (Phase 6 cutover): `status`
                # is the canonical field (the mDNS control/status name); the
                # radiod id is derived from it.  The legacy `id` +
                # `radiod_status` pair was removed everywhere else (runtime
                # resolve_radiod_status, `config show`, the init template) --
                # require `status` here too so the show -> edit -> apply
                # round-trip is consistent.
                if not block.get("status"):
                    print(f"config apply: [[radiod]][{i}].status is required (mDNS hostname)",
                          file=sys.stderr)
                    return 2
            # Reject duplicate status names -- recorder.py keys per-instance
            # state on the radiod id, which is derived from status.
            statuses = [b["status"] for b in fields]
            if len(statuses) != len(set(statuses)):
                print(f"config apply: [[radiod]] has duplicate status names: {statuses}",
                      file=sys.stderr)
                return 2
            continue

        if not isinstance(fields, dict):
            print(f"config apply: [{section}] must be a table, got {type(fields).__name__}",
                  file=sys.stderr)
            return 2
        default_section = DEFAULTS.get(section, {})
        for key, val in fields.items():
            default = default_section.get(key)
            if default is None or val is None:
                continue
            if isinstance(default, bool):
                if not isinstance(val, bool):
                    print(f"config apply: [{section}].{key} expects bool, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2
            elif isinstance(default, int) and not isinstance(default, bool):
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    print(f"config apply: [{section}].{key} expects number, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2
                fields[key] = int(val) if isinstance(default, int) else float(val)
            elif isinstance(default, str):
                if not isinstance(val, str):
                    print(f"config apply: [{section}].{key} expects string, got {type(val).__name__}",
                          file=sys.stderr)
                    return 2

    # Merge with the existing file.  Scalar tables deep-merge so a
    # partial payload preserves untouched fields.  [[radiod]] is a
    # list-of-dicts; overlay-wins (the operator's edited list
    # replaces the file's list) because deep-merge doesn't compose
    # for arrays-of-tables.  _deep_merge already does this: nested
    # dicts merge, everything else (including lists) is overwritten.
    if config_path.is_file():
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)
    else:
        existing = {}
    merged = _deep_merge(existing, payload)

    # Run merged config through load_config's validators by writing to a
    # tempfile and round-tripping (since load_config takes a path).  Catches
    # radiod_lifetime_frames range, etc.  Skip if there's no [[radiod]] yet
    # (fresh-install case where the wizard runs against the template).
    if merged.get("radiod"):
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".toml", prefix="meteor-scatter-validate.", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tmp_path.write_text(_serialize_toml(merged), encoding="utf-8")
            try:
                load_config(tmp_path)
            except (ValueError, tomllib.TOMLDecodeError) as exc:
                print(f"config apply: rejected by load_config: {exc}",
                      file=sys.stderr)
                return 2
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    text = _serialize_toml(merged)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o644)
    except PermissionError:
        pass
    tmp.replace(config_path)
    print(f"wrote {config_path}")
    return 0


# ---------------------------------------------------------------------------
# TOML serialization (hand-emitted; no tomli-w dep).
#
# Handles: scalars (str/int/float/bool), nested dicts as [section.child]
# blocks, and arrays-of-tables as `[[section]]` headers (needed for the
# [[radiod]] blocks that pass through unchanged from the existing file).
# Arrays of scalars (freqs_hz) render as inline arrays so they fit on
# one line per band.
# ---------------------------------------------------------------------------

def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        if "." not in s and "e" not in s and "E" not in s:
            s += ".0"
        return s
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported TOML scalar type: {type(v).__name__}")


def _toml_inline_array(arr: list) -> str:
    """Render a list of scalars as a single-line TOML array."""
    return "[" + ", ".join(_toml_scalar(x) for x in arr) + "]"


def _serialize_toml(d: dict, parent: str = "") -> str:
    """Serialize ``d`` to a deterministic TOML string.

    Section order matches DEFAULTS where possible (paths, processing)
    so successive writes diff cleanly; sections not in DEFAULTS
    (station, radiod) follow.  [[radiod]] blocks render as
    array-of-tables.
    """
    # Stable order: DEFAULTS first, then well-known non-default
    # sections in a fixed order, then anything else alphabetical.
    default_keys  = [k for k in DEFAULTS.keys() if k in d]
    well_known    = [k for k in ("station", "radiod") if k in d and k not in default_keys]
    extras        = sorted(k for k in d.keys() if k not in default_keys and k not in well_known)
    ordered_keys  = default_keys + well_known + extras

    out_lines: list[str] = []
    for section_name in ordered_keys:
        section = d[section_name]
        # [[section]] array-of-tables (e.g. [[radiod]]).
        if isinstance(section, list):
            header = f"{parent}.{section_name}" if parent else section_name
            for block in section:
                if not isinstance(block, dict):
                    raise TypeError(f"[[{header}]] entries must be tables, got {type(block).__name__}")
                out_lines.append("")
                out_lines.append(f"[[{header}]]")
                out_lines.extend(_serialize_section_body(block, header))
            continue

        if not isinstance(section, dict):
            out_lines.append(f"{section_name} = {_toml_scalar(section)}")
            continue

        header = f"{parent}.{section_name}" if parent else section_name
        out_lines.append("")
        out_lines.append(f"[{header}]")
        out_lines.extend(_serialize_section_body(section, header))

    return "\n".join(out_lines).lstrip("\n") + "\n"


def _serialize_section_body(section: dict, header: str) -> list[str]:
    """Emit one section's body: scalars first, then nested dicts as sub-tables."""
    lines: list[str] = []
    scalars  = {k: v for k, v in section.items() if not isinstance(v, dict)}
    children = {k: v for k, v in section.items() if isinstance(v, dict)}

    # DEFAULTS key order for known sections; alphabetical for the rest.
    default_section = DEFAULTS.get(header.split(".")[0], {})
    default_keys = [k for k in default_section.keys() if k in scalars]
    extra_keys = sorted(k for k in scalars.keys() if k not in default_section)
    for k in default_keys + extra_keys:
        v = scalars[k]
        if isinstance(v, list):
            lines.append(f"{k} = {_toml_inline_array(v)}")
        else:
            lines.append(f"{k} = {_toml_scalar(v)}")

    for child_name, child in children.items():
        sub_header = f"{header}.{child_name}"
        lines.append("")
        lines.append(f"[{sub_header}]")
        lines.extend(_serialize_section_body(child, sub_header))
    return lines


# ---------------------------------------------------------------------------
# Argparse wiring (called from cli.py)
# ---------------------------------------------------------------------------

def add_show_apply_subparsers(cfg_sub: argparse._SubParsersAction,
                              *, common=None) -> None:
    """Register `config show` and `config apply` on the config subparser.

    Called from cli.py so the argparse tree stays in one place.
    ``common`` is the local _add_common(sub) helper from cli.py,
    hand-passed so we don't import private helpers across modules.
    """
    sub_show = cfg_sub.add_parser("show",
        help="emit the current config as JSON")
    sub_show.add_argument("--json", action="store_true",
        help="emit JSON (the only output format today)")
    sub_show.add_argument("--defaults", action="store_true",
        help="merge with DEFAULTS so every default key is present")
    if common:
        common(sub_show)

    sub_apply = cfg_sub.add_parser("apply",
        help="apply a JSON dict from stdin to the config file")
    sub_apply.add_argument("--json", action="store_true",
        help="read JSON from stdin (the only input format today)")
    sub_apply.add_argument("path", nargs="?", default="-",
        help="JSON file path or '-' for stdin (default)")
    if common:
        common(sub_apply)


# ---------------------------------------------------------------------------
# env show / env apply -- /etc/meteor-scatter/env/<radiod_id>.env
# ---------------------------------------------------------------------------
#
# meteor-scatter reads its per-instance env file via systemd's
# EnvironmentFile=-/etc/meteor-scatter/env/%i.env directive (where %i is
# the radiod_id of the [[radiod]] block this instance serves).  The file
# is where upload-destination knobs will live once the wsprdaemon.org
# transport is wired (Phase 3); in the current deposit-only build there
# are no managed upload knobs, so the wizard manages no keys here yet.
# Sigmond's coordination.env (/etc/sigmond/coordination.env) is host-wide
# and sigmond-owned; the wizard reads from it (e.g. for the SQLite sink
# path) but never writes there.

_ENV_DIR = Path("/etc/meteor-scatter/env")

# Keys the wizard / `env apply` will write.  Empty in the deposit-only
# build — the PSKReporter-era delivery knobs were removed with the
# uploader, and the wsprdaemon.org upload knobs don't exist yet (Phase 3
# will repopulate this set).  `env apply` therefore rejects any payload
# key until then, so a stale knob can't silently do nothing.
_ENV_WRITABLE_KEYS: "set[str]" = set()


def _env_file_path(instance: str) -> Path:
    return _ENV_DIR / f"{instance}.env"


def _parse_env_file(path: Path) -> "dict[str, str]":
    """Read a systemd-style KEY=VALUE env file.  Skips comments and blanks.
    Values may be quoted with " or '; we strip them but don't try to
    handle shell-style escapes."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


def _serialize_env_file(d: "dict[str, str]") -> str:
    """Serialize back to KEY=VALUE lines.  We don't preserve the source
    file's comments or ordering -- env files are flat and the wizard
    is the canonical writer."""
    lines: list[str] = []
    for key in sorted(d.keys()):
        val = d[key]
        # Quote if the value has whitespace, equals sign, or shell metachars.
        if any(c in val for c in ' \t"\'#=$`'):
            quoted = '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append(f"{key}={quoted}")
        else:
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


def _validate_env_payload(payload: dict) -> Optional[str]:
    """Returns an error message, or None if the payload is acceptable.

    Only keys in _ENV_WRITABLE_KEYS may appear; other keys would
    require us to either delete them or pass them through (we choose
    to reject so the operator notices typos)."""
    bad = set(payload.keys()) - _ENV_WRITABLE_KEYS
    if bad:
        return (f"env apply: unknown / unmanaged keys: {sorted(bad)} "
                f"(allowed: {sorted(_ENV_WRITABLE_KEYS)}).  This build manages "
                f"no env knobs yet — the wsprdaemon.org upload knobs arrive "
                f"with the upload transport in Phase 3.")
    return None


def cmd_env_show(args) -> int:
    """Emit /etc/meteor-scatter/env/<instance>.env as JSON on stdout."""
    instance = args.instance
    if not instance:
        print("env show: --instance <radiod_id> is required", file=sys.stderr)
        return 2
    parsed = _parse_env_file(_env_file_path(instance))
    json.dump(parsed, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_env_apply(args) -> int:
    """Read a JSON dict on stdin, validate, atomically write the env file.

    Merge semantics: keys in the payload OVERRIDE / SET the corresponding
    entries in the existing file; other keys pass through unchanged.
    To DELETE a key entirely, the payload may set its value to JSON null.
    """
    instance = args.instance
    if not instance:
        print("env apply: --instance <radiod_id> is required", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"env apply: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"env apply: top-level JSON must be an object, got {type(payload).__name__}",
              file=sys.stderr)
        return 2

    # Separate deletes (null values) from sets.
    deletes = {k for k, v in payload.items() if v is None}
    sets    = {k: v for k, v in payload.items() if v is not None}

    err = _validate_env_payload(sets)
    if err:
        print(f"env apply: {err}", file=sys.stderr)
        return 2

    env_path = _env_file_path(instance)
    existing = _parse_env_file(env_path)
    for k in deletes:
        existing.pop(k, None)
    existing.update({k: str(v) for k, v in sets.items()})

    env_path.parent.mkdir(parents=True, exist_ok=True)
    text = _serialize_env_file(existing)
    tmp = env_path.with_suffix(env_path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o644)
    except PermissionError:
        pass
    tmp.replace(env_path)
    print(f"wrote {env_path}")
    return 0


def add_env_subparsers(subparsers: argparse._SubParsersAction,
                       *, common=None) -> None:
    """Register top-level `env show` / `env apply` subcommands.

    Called from cli.py.  Lives in configurator.py so all schema-aware
    env-file machinery is colocated with the TOML show/apply code.
    """
    sub_env = subparsers.add_parser("env",
        help="read or write /etc/meteor-scatter/env/<instance>.env")
    env_sub = sub_env.add_subparsers(dest="env_command")

    sub_env_show = env_sub.add_parser("show",
        help="emit the per-instance env file as JSON")
    sub_env_show.add_argument("--json", action="store_true",
        help="emit JSON (the only output format today)")
    sub_env_show.add_argument("--instance", required=True,
        help="radiod_id of the instance to inspect")
    if common:
        common(sub_env_show)

    sub_env_apply = env_sub.add_parser("apply",
        help="apply a JSON dict from stdin to the per-instance env file")
    sub_env_apply.add_argument("--json", action="store_true",
        help="read JSON from stdin (the only input format today)")
    sub_env_apply.add_argument("--instance", required=True,
        help="radiod_id of the instance to write")
    sub_env_apply.add_argument("path", nargs="?", default="-",
        help="JSON file path or '-' for stdin (default)")
    if common:
        common(sub_env_apply)
