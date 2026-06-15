#!/bin/bash
#
# meteor-scatter config wizard (whiptail).
#
# Called by `meteor-scatter config init` and `meteor-scatter config edit`
# when stdout is a TTY and whiptail is installed.  Drives the operator
# through Station / Paths / Processing dialogs, validates inline, and
# writes the result through `meteor-scatter config apply --json -` so
# the schema / type / range checks happen in Python.
#
# [[radiod]] arrays-of-tables and per-band freqs_hz lists are NOT
# editable through this wizard -- whiptail can't naturally express
# either.  The main menu has an "Edit raw TOML" item that opens the
# config in $EDITOR for the rare cases that need it.
#
# Usage:
#   config-wizard.sh init [--config <path>]
#   config-wizard.sh edit [--config <path>]
#
# Env (set by configurator.py before exec):
#   METEOR_SCATTER_CLI         path to the meteor-scatter binary to use
#   METEOR_SCATTER_HELP_TOML   path to config/help.toml
#
# Reads (read-only) for pre-fills:
#   /etc/sigmond/coordination.env   STATION_CALL / STATION_GRID / etc. (§14.3)
#

set -euo pipefail

MODE="${1:-init}"; shift || true
CONFIG_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2 ;;
        *) echo "config-wizard: unknown arg: $1" >&2; exit 2 ;;
    esac
done

METEOR_SCATTER="${METEOR_SCATTER_CLI:-meteor-scatter}"
HELP_TOML="${METEOR_SCATTER_HELP_TOML:-/opt/git/sigmond/meteor-scatter/config/help.toml}"
COORD_ENV="/etc/sigmond/coordination.env"

# -------- shared shell helpers ----------------------------------------
#
# Source sigmond's Tier-1 wizard helpers (preflight_or_exit_2, _info /
# _warn / _err, recommended HEIGHT/WIDTH/LIST_HEIGHT/BACKTITLE
# defaults).  The Python dispatcher (meteor_scatter.configurator
# ._exec_wizard) sets SIGMOND_WIZARD_LIB_SH in the env; the :- default
# below covers direct-invocation safety (e.g. running this script by
# hand during development).
#
# If sigmond's lib isn't installed, define the helpers inline as a
# fallback so this script still works on a host that's missing sigmond.
SIGMOND_WIZARD_LIB_SH="${SIGMOND_WIZARD_LIB_SH:-/opt/git/sigmond/sigmond/lib/sigmond/wizard_dispatch/wizard_dispatch.sh}"
if [[ -r "$SIGMOND_WIZARD_LIB_SH" ]]; then
    # shellcheck disable=SC1090
    . "$SIGMOND_WIZARD_LIB_SH"
else
    HEIGHT=20; WIDTH=78; LIST_HEIGHT=10
    _info() { printf '  %s\n'                "$*" >&2; }
    _warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
    _err()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }
    preflight_or_exit_2() {
        command -v whiptail >/dev/null 2>&1 \
            || { _err "whiptail not on PATH"; exit 2; }
        [[ -t 1 ]] \
            || { _err "stdout is not a TTY"; exit 2; }
    }
fi

# Override BACKTITLE to be client-specific (the lib's default is
# generic for the rare case where a client forgets).
BACKTITLE="meteor-scatter configuration"

# -------- preflight ----------------------------------------------------

if ! command -v whiptail >/dev/null 2>&1; then
    cat <<EOF >&2
meteor-scatter config: whiptail is not installed on this host.

The interactive wizard requires it.  Install with:

    sudo apt install whiptail

Or use the legacy stdin-prompt path with:

    meteor-scatter config $MODE --non-interactive
EOF
    exit 1
fi
preflight_or_exit_2

# -------- helpers ----------------------------------------------------

# Pre-fill from sigmond's coordination.env (read-only).
seed_from_coord_env() {
    local key="$1"
    [[ -r "$COORD_ENV" ]] || return 0
    sed -nE "s|^[[:space:]]*${key}=([\"']?)([^\"']*)\\1[[:space:]]*\$|\\2|p" \
        "$COORD_ENV" | tail -1
}

# Read one scalar out of the current effective config via JSON.
config_get() {
    local section="$1" key="$2"
    "$METEOR_SCATTER" config show --json --defaults ${CONFIG_PATH:+--config "$CONFIG_PATH"} 2>/dev/null \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = d.get('$section', {}).get('$key', '')
if isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
"
}

# In-session lookup: SCRATCH_JSON (pending edits) first, then disk.
current_value() {
    local section="$1" key="$2"
    local scratch_val
    scratch_val=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
v = d.get('$section', {}).get('$key', None)
if v is None:
    pass
elif isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
")
    if [[ -n "$scratch_val" ]]; then
        echo "$scratch_val"
    else
        config_get "$section" "$key"
    fi
}

# Pull help.toml's title/help/example/validator_hint/required for a key.
help_get() {
    local dotted="$1" attr="$2"
    [[ -r "$HELP_TOML" ]] || return 0
    python3 -c "
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open('$HELP_TOML', 'rb') as f:
    d = tomllib.load(f)
node = d
for part in '$dotted'.split('.'):
    if not isinstance(node, dict):
        node = {}
        break
    node = node.get(part, {})
if isinstance(node, dict):
    v = node.get('$attr', '')
    if isinstance(v, bool):
        print('true' if v else 'false')
    else:
        print(v)
" 2>/dev/null
}

# -------- validators --------------------------------------------------

valid_callsign()       { [[ "$1" =~ ^[A-Za-z0-9/]{1,9}$ ]]; }
# Maidenhead 6-10 chars: field (A-R) + square (0-9) + alpha-num
# subsquare etc.  Accept case-insensitive; canonicalize before write.
valid_grid()           { [[ "$1" =~ ^[A-Ra-r]{2}[0-9]{2}[A-Za-z0-9]{2,8}$ ]]; }
valid_path_absolute()  { [[ "$1" == /* ]]; }
valid_path_executable(){ [[ "$1" == /* && -x "$1" ]]; }
valid_bool()           { [[ "$1" =~ ^(true|false)$ ]]; }
valid_nonneg_int()     { [[ "$1" =~ ^[0-9]+$ ]]; }
valid_always()         { return 0; }   # for free-form text / no-validate fields

# -------- ask() ------------------------------------------------------

# Returns the entered value on stdout.
#
# Cancel/Esc behaviour depends on help.toml's `required` flag:
#   required = true  -> Cancel returns 1; caller aborts the section.
#   required = false -> Cancel echoes the pre-fill and returns 0
#                       (operator-perceived skip-and-keep).
#
# Whiptail mis-parses defaults starting with '-' as flags.  Workaround:
# pass an empty default and surface the current value in the body when
# the pre-fill starts with '-'.  (meteor-scatter's fields don't normally
# carry leading dashes, but reusing the mag-recorder fix keeps the
# wizard scaffolding consistent in case someone sets a negative
# radiod_lifetime_frames or similar future field.)
#
# Args: dotted_key  current_value  validator_fn  validator_args...
ask() {
    local dotted="$1" current="$2" validator="$3"; shift 3
    local extra_args=("$@")

    local title; title=$(help_get "$dotted" "title")
    local help;  help=$(help_get  "$dotted" "help")
    local example;  example=$(help_get "$dotted" "example")
    local hint;  hint=$(help_get  "$dotted" "validator_hint")
    local required; required=$(help_get "$dotted" "required")
    [[ -z "$required" ]] && required="true"

    [[ -z "$title" ]] && title="$dotted"
    local body="$help"
    [[ -n "$hint"    ]] && body+=$'\n\nFormat: '"$hint"
    [[ -n "$example" ]] && body+=$'\n''Example: '"$example"
    if [[ "$required" != "true" ]]; then
        body+=$'\n\n''(Optional -- Cancel to keep current value.)'
    fi

    local effective_default="$current"
    if [[ "$current" == -* ]]; then
        body+=$'\n\nCurrent value: '"$current"$'\n''(Leave the box empty to keep it.)'
        effective_default=""
    fi

    local entered
    while :; do
        if ! entered=$(whiptail \
                --title "$title" \
                --backtitle "$BACKTITLE" \
                --inputbox "$body" \
                "$HEIGHT" "$WIDTH" \
                "$effective_default" 3>&1 1>&2 2>&3); then
            if [[ "$required" == "true" ]]; then
                return 1
            fi
            echo "$current"
            return 0
        fi
        entered="${entered## }"; entered="${entered%% }"  # trim
        if [[ -z "$entered" && "$current" == -* ]]; then
            echo "$current"
            return 0
        fi
        if "$validator" "$entered" "${extra_args[@]}"; then
            echo "$entered"
            return 0
        fi
        whiptail --title "Invalid value" \
                 --backtitle "$BACKTITLE" \
                 --msgbox $'That value didn'\''t match the expected format.\n\n'"Hint: ${hint:-(see help text)}" \
                 12 "$WIDTH"
        current="$entered"
        effective_default="$entered"
        [[ "$current" == -* ]] && effective_default=""
    done
}

# Yes/no helper -- shared shape with --yesno-button labels.
ask_yesno() {
    local title="$1" body="$2"
    whiptail --title "$title" --backtitle "$BACKTITLE" --yesno "$body" 12 "$WIDTH"
}

# -------- screens ----------------------------------------------------

welcome_screen() {
    local body
    if [[ "$MODE" == "init" ]]; then
        body="Welcome to the meteor-scatter configuration wizard.

You'll see a menu of sections (Station, Paths, Processing); pick
any section to fill in, then return to the menu.  Pick 'Apply'
when you're done to write everything in one go, or 'Cancel' to
discard pending changes and exit.

Pre-fills come from /etc/sigmond/coordination.env (if present) and
your current /etc/meteor-scatter/meteor-scatter-config.toml.  Inside a
section, pressing Cancel drops back to the menu (not all the way
out) -- effectively a 'back' button.

[[radiod]] blocks and per-band freqs_hz lists aren't editable
here -- pick 'Edit raw TOML' from the menu to open the file in
\$EDITOR (or hand-edit it later)."
    else
        body="Edit the current meteor-scatter configuration.

You'll see a menu of sections (Station, Paths, Processing) with
current values shown inline.  Pick any section to edit, then
return to the menu.  Pick 'Apply' to write changes or 'Cancel'
to discard them.  Inside a section, pressing Cancel drops back
to the menu (not out).

For [[radiod]] blocks and per-band freqs_hz lists, pick 'Edit
raw TOML' from the menu to open the file in \$EDITOR."
    fi
    whiptail --title "meteor-scatter configuration wizard" \
             --backtitle "$BACKTITLE" \
             --yesno "$body"$'\n\n'"Continue?" \
             "$HEIGHT" "$WIDTH"
}

collect_station() {
    local callsign grid antenna
    callsign=$(current_value station callsign)
    [[ -z "$callsign" ]] && callsign="$(seed_from_coord_env STATION_CALL)"
    [[ -z "$callsign" ]] && callsign="$(seed_from_coord_env STATION_CALLSIGN)"
    grid=$(current_value station grid_square)
    [[ -z "$grid" ]] && grid="$(seed_from_coord_env STATION_GRID)"
    antenna=$(current_value station antenna)

    callsign=$(ask station.callsign    "$callsign" valid_callsign) || return 1
    callsign="${callsign^^}"
    grid=$(ask     station.grid_square "$grid"     valid_grid)     || return 1
    # Canonical Maidenhead: field uppercase, square unchanged, subsquare lower.
    {
        local _f="${grid:0:2}" _s="${grid:2:2}" _rest="${grid:4}"
        grid="${_f^^}${_s}${_rest,,}"
    }
    # antenna is free-form; no validator.  Optional -- skip-on-Cancel via help.toml.
    antenna=$(ask station.antenna "$antenna" valid_always) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('station', {}).update({
    'callsign':    '$callsign',
    'grid_square': '$grid',
    'antenna':     '''$antenna''',
})
print(json.dumps(d))
")
}

collect_paths() {
    local spool log decoder_kind decoder_path pskreporter pskreporter_tcp keep_wav
    spool=$(current_value         paths spool_dir)
    log=$(current_value           paths log_dir)
    decoder_kind=$(current_value  paths decoder_kind)
    [[ -z "$decoder_kind" ]] && decoder_kind="decode_ft8"
    decoder_path=$(current_value  paths decoder_decode_ft8)
    [[ -z "$decoder_path" ]] && decoder_path="/usr/local/bin/decode_ft8"
    pskreporter=$(current_value     paths pskreporter)
    pskreporter_tcp=$(current_value paths pskreporter_tcp)
    [[ -z "$pskreporter_tcp" ]] && pskreporter_tcp="true"
    keep_wav=$(current_value      paths keep_wav)

    spool=$(ask         paths.spool_dir         "$spool"        valid_path_absolute) || return 1
    log=$(ask           paths.log_dir           "$log"          valid_path_absolute) || return 1
    decoder_kind=$(ask  paths.decoder_kind      "$decoder_kind" valid_always)        || return 1
    decoder_path=$(ask  paths.decoder_decode_ft8 "$decoder_path" valid_path_absolute) || return 1
    pskreporter=$(ask   paths.pskreporter       "$pskreporter"  valid_path_absolute) || return 1

    # Bool fields presented as yesno screens.
    local pskreporter_tcp_new keep_wav_new
    if ask_yesno "$(help_get paths.pskreporter_tcp title)" \
                 "$(help_get paths.pskreporter_tcp help)"$'\n\n'"Current value: $pskreporter_tcp"$'\n\n'"Use TCP transport?"; then
        pskreporter_tcp_new=true
    else
        pskreporter_tcp_new=false
    fi
    pskreporter_tcp="$pskreporter_tcp_new"

    if ask_yesno "$(help_get paths.keep_wav title)" \
                 "$(help_get paths.keep_wav help)"$'\n\n'"Current value: $keep_wav"$'\n\n'"Keep WAV slices after decode?"; then
        keep_wav_new=true
    else
        keep_wav_new=false
    fi
    keep_wav="$keep_wav_new"

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('paths', {}).update({
    'spool_dir':         '$spool',
    'log_dir':           '$log',
    'decoder_kind':      '$decoder_kind',
    'decoder_decode_ft8': '$decoder_path',
    'pskreporter':       '$pskreporter',
    # bools arrived as bash strings 'true'/'false'; coerce.
    'pskreporter_tcp':   ('$pskreporter_tcp' == 'true'),
    'keep_wav':          ('$keep_wav' == 'true'),
})
print(json.dumps(d))
")
}

collect_processing() {
    local lifetime
    lifetime=$(current_value processing radiod_lifetime_frames)
    lifetime=$(ask processing.radiod_lifetime_frames "$lifetime" valid_nonneg_int) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('processing', {}).update({
    'radiod_lifetime_frames': int('$lifetime'),
})
print(json.dumps(d))
")
}

# Return a comma-separated summary of [[radiod]] ids (or "(none)").
# Reads from SCRATCH_JSON first (in-session edits), then disk.
#
# All bash <-> Python communication uses env vars (not string
# interpolation into the python -c source).  Earlier draft had
# `( and ['--config', ''] or [])` as a Python syntax error when
# CONFIG_PATH was empty -- env-var passing avoids the whole class
# of bug.
radiod_ids_summary() {
    SCRATCH="$SCRATCH_JSON" CFG="$CONFIG_PATH" PSK="$METEOR_SCATTER" python3 <<'PYEOF' 2>/dev/null
import json, os, subprocess
try:
    scratch = json.loads(os.environ.get('SCRATCH') or '{}')
except Exception:
    scratch = {}
if 'radiod' in scratch:
    blocks = scratch['radiod']
else:
    args = [os.environ['PSK'], 'config', 'show', '--json', '--defaults']
    if os.environ.get('CFG'):
        args += ['--config', os.environ['CFG']]
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL).decode()
        blocks = json.loads(out).get('radiod', [])
    except Exception:
        blocks = []
ids = [b.get('status', '?') for b in blocks if isinstance(b, dict)]
print(','.join(ids) if ids else '(none)')
PYEOF
}

# Pull the current full [[radiod]] list (from SCRATCH_JSON or disk)
# as a JSON array on stdout.
radiod_list_json() {
    SCRATCH="$SCRATCH_JSON" CFG="$CONFIG_PATH" PSK="$METEOR_SCATTER" python3 <<'PYEOF'
import json, os, subprocess
try:
    scratch = json.loads(os.environ.get('SCRATCH') or '{}')
except Exception:
    scratch = {}
if 'radiod' in scratch:
    print(json.dumps(scratch['radiod']))
else:
    args = [os.environ['PSK'], 'config', 'show', '--json', '--defaults']
    if os.environ.get('CFG'):
        args += ['--config', os.environ['CFG']]
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL).decode()
        print(json.dumps(json.loads(out).get('radiod', [])))
    except Exception:
        print('[]')
PYEOF
}

# Update the SCRATCH_JSON [[radiod]] list at index ``idx`` (or append
# when idx == -1 / >= len) with id + radiod_status; preserves any
# existing nested [radiod.ft8] / [radiod.ft4] sub-tables and freqs_hz.
radiod_block_update() {
    local idx="$1" new_id="$2" new_status="$3"
    local current_blocks
    current_blocks=$(radiod_list_json)
    SCRATCH_JSON=$(SCRATCH="$SCRATCH_JSON" BLOCKS="$current_blocks" \
                   IDX="$idx" NEW_ID="$new_id" NEW_STATUS="$new_status" \
                   python3 <<'PYEOF'
import json, os
try:
    d = json.loads(os.environ.get('SCRATCH') or '{}')
except Exception:
    d = {}
blocks = d.get('radiod')
if blocks is None:
    blocks = json.loads(os.environ.get('BLOCKS') or '[]')
idx = int(os.environ['IDX'])
new_id     = os.environ['NEW_ID']
new_status = os.environ['NEW_STATUS']
if idx == -1 or idx >= len(blocks):
    blocks.append({'id': new_id, 'radiod_status': new_status})
else:
    blocks[idx]['id']            = new_id
    blocks[idx]['radiod_status'] = new_status
d['radiod'] = blocks
print(json.dumps(d))
PYEOF
)
}

# Delete the [[radiod]] block at index idx (operator confirmed first).
radiod_block_delete() {
    local idx="$1"
    local current_blocks
    current_blocks=$(radiod_list_json)
    SCRATCH_JSON=$(SCRATCH="$SCRATCH_JSON" BLOCKS="$current_blocks" IDX="$idx" \
                   python3 <<'PYEOF'
import json, os
try:
    d = json.loads(os.environ.get('SCRATCH') or '{}')
except Exception:
    d = {}
blocks = d.get('radiod')
if blocks is None:
    blocks = json.loads(os.environ.get('BLOCKS') or '[]')
del blocks[int(os.environ['IDX'])]
d['radiod'] = blocks
print(json.dumps(d))
PYEOF
)
}

# Look up one field of one [[radiod]] block by index, from the
# operator-visible block list (SCRATCH first, then disk).  Returns
# empty string if out of range.  Used by collect_radiod to pre-fill
# the edit dialogs.
radiod_block_get_field() {
    local idx="$1" field="$2"
    local current_blocks
    current_blocks=$(radiod_list_json)
    BLOCKS="$current_blocks" IDX="$idx" FIELD="$field" python3 <<'PYEOF' 2>/dev/null
import json, os
try:
    blocks = json.loads(os.environ.get('BLOCKS') or '[]')
except Exception:
    blocks = []
idx = int(os.environ['IDX'])
if 0 <= idx < len(blocks):
    print(blocks[idx].get(os.environ['FIELD'], ''))
PYEOF
}

# Picker for radiod_status that surfaces LAN radiods from sigmond's
# environment cache (/var/lib/sigmond/environment-cache.json).
# Operator picks a real cached entry by its advertised name instead
# of typing a hostname from memory.  Falls through to the free-form
# inputbox for "(manual)" or when the cache is missing / empty.
#
# Lifted from wspr-recorder's wizard (commit 9bf605f, Rob Robinett).
# Differences:
#   - accepts BOTH `source=='mdns'` and `source=='multicast'` because
#     sigmond has two discovery code paths and both produce kind=radiod
#     observations.  wspr-recorder's port only checks 'mdns', which
#     yields an empty cache on hosts using the multicast discovery
#     code path (this is one of them).
#   - falls back through display label preferences:
#         mdns_name (advertised label like "AC0G @EM38ww B1 T3FD")
#         -> id (radiod_id from the [[radiod]] block)
#         -> endpoint (bare hostname)
#
# Args:  $1 = current value (the pre-fill if operator picks manual)
# Output (stdout): the chosen hostname, or empty on cancel.
pick_radiod_status() {
    local current="${1:-}"
    local cache="${SIGMOND_ENV_CACHE:-/var/lib/sigmond/environment-cache.json}"

    # Pull cached radiod observations as "<endpoint>|<label>" lines.
    local -a cache_lines=()
    if [[ -r "$cache" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && cache_lines+=("$line")
        done < <(CACHE="$cache" python3 <<'PYEOF' 2>/dev/null
import json, os
try:
    data = json.load(open(os.environ['CACHE']))
except Exception:
    raise SystemExit(0)
seen = set()
for obs in data.get('observations') or []:
    if obs.get('source') not in ('mdns', 'multicast'):
        continue
    if obs.get('kind') != 'radiod' or not obs.get('ok', True):
        continue
    endpoint = (obs.get('endpoint') or '').rsplit(':', 1)[0]
    if not endpoint or endpoint in seen:
        continue
    seen.add(endpoint)
    fields = obs.get('fields') or {}
    label = (fields.get('mdns_name')
             or obs.get('id')
             or endpoint).strip()
    print(f'{endpoint}|{label}')
PYEOF
)
    fi

    # Build the whiptail menu.  Each cached entry gets a "cached-N" tag
    # so we can map back to the address after selection.
    local -A tag_to_addr=()
    local -a menu_items=()
    local i=0 addr label tag desc
    for line in "${cache_lines[@]}"; do
        addr="${line%%|*}"
        label="${line#*|}"
        tag="cached-$i"
        tag_to_addr["$tag"]="$addr"
        desc="$label"
        if [[ "$addr" == "$current" ]]; then
            desc="$desc  (current)"
        else
            desc="$desc  (cached)"
        fi
        menu_items+=("$tag" "$desc")
        i=$((i + 1))
    done
    # If the current value isn't in the cache, surface it explicitly so
    # the operator can re-pick it without retyping.
    if [[ -n "$current" ]]; then
        local found=0
        for tag in "${!tag_to_addr[@]}"; do
            [[ "${tag_to_addr[$tag]}" == "$current" ]] && { found=1; break; }
        done
        if [[ $found -eq 0 ]]; then
            tag_to_addr["cached-current"]="$current"
            menu_items+=("cached-current" "$current  (current; not in cache)")
        fi
    fi
    menu_items+=("manual" "Enter another mDNS hostname by hand...")
    menu_items+=("cancel" "(cancel: keep current value)")

    local hint="Pick the radiod multicast control plane this recorder should join."
    if [[ ${#cache_lines[@]} -eq 0 ]]; then
        hint+=$'\n\nNo radiods in sigmond\'s environment cache.\nRun `smd environment probe` to populate it, or pick Manual.'
    fi

    local pick
    if ! pick=$(whiptail \
            --title "Radiod status address" \
            --backtitle "$BACKTITLE" \
            --menu "$hint" \
            "$HEIGHT" "$WIDTH" "$LIST_HEIGHT" \
            "${menu_items[@]}" 3>&1 1>&2 2>&3); then
        # Esc/Cancel = keep current value (silently).
        echo "$current"
        return 0
    fi

    case "$pick" in
        cancel)
            echo "$current"
            ;;
        manual)
            # Fall through to the same inputbox the legacy path used.
            ask radiod.radiod_status "$current" valid_mdns_host || return 1
            ;;
        cached-*)
            echo "${tag_to_addr[$pick]:-$current}"
            ;;
        *)
            echo "$current"
            ;;
    esac
}

# meteor-scatter's radiod id has the same shape as a callsign in spirit
# -- letters, digits, hyphens.  Let's not block legitimate values.
valid_radiod_id()   { [[ "$1" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; }
valid_mdns_host()   { [[ "$1" =~ ^[A-Za-z0-9._-]{1,253}$ ]]; }

collect_radiod() {
    while :; do
        local blocks_json
        blocks_json=$(radiod_list_json)

        # Build menu items as a bash array.  Each block becomes a
        # (tag, description) pair; tag = index (or "new" / "done").
        # No `eval` -- the array expands directly into whiptail's argv.
        local -a menu_items=()
        local count i rid status ft8 ft4
        count=$(BLOCKS="$blocks_json" python3 <<'PYEOF' 2>/dev/null
import json, os
print(len(json.loads(os.environ.get('BLOCKS') or '[]')))
PYEOF
)
        for ((i = 0; i < count; i++)); do
            rid=$(BLOCKS="$blocks_json" IDX="$i" python3 <<'PYEOF' 2>/dev/null
import json, os
blocks = json.loads(os.environ.get('BLOCKS') or '[]')
b = blocks[int(os.environ['IDX'])]
ft8 = len((b.get('ft8') or {}).get('freqs_hz') or [])
ft4 = len((b.get('ft4') or {}).get('freqs_hz') or [])
print(f"{b.get('id', '?')}  status={b.get('radiod_status', '?')}  ft8={ft8} ft4={ft4}")
PYEOF
)
            menu_items+=("$i" "$rid")
        done
        menu_items+=("new"  "(add a new [[radiod]] block)")
        menu_items+=("done" "(back to main menu)")

        local pick
        if ! pick=$(whiptail \
                --title "Radiod blocks" \
                --backtitle "$BACKTITLE" \
                --menu "Pick a [[radiod]] block to edit, or add a new one.

freqs_hz lists stay in the raw TOML -- use 'Edit-TOML' from the
main menu for those." \
                "$HEIGHT" "$WIDTH" "$LIST_HEIGHT" \
                "${menu_items[@]}" 3>&1 1>&2 2>&3); then
            return 1
        fi

        case "$pick" in
            done) return 0 ;;
            new)
                local new_id new_status
                new_id=$(ask     radiod.id            "" valid_radiod_id) || continue
                new_status=$(pick_radiod_status      "") || continue
                # pick_radiod_status echoes empty on cancel-with-no-current;
                # treat that as abort-this-block.
                [[ -z "$new_status" ]] && continue
                radiod_block_update -1 "$new_id" "$new_status"
                ;;
            *)
                # Numeric pick = index into the block list.
                local cur_id cur_status
                cur_id=$(radiod_block_get_field "$pick" id)
                cur_status=$(radiod_block_get_field "$pick" radiod_status)
                local subpick
                if ! subpick=$(whiptail \
                        --title "Block '$cur_id'" \
                        --backtitle "$BACKTITLE" \
                        --menu "Edit or delete this [[radiod]] block?" \
                        "$HEIGHT" "$WIDTH" 3 \
                        "edit"   "change id / radiod_status" \
                        "delete" "remove this block entirely" \
                        "back"   "(back to block list)" \
                        3>&1 1>&2 2>&3); then
                    continue
                fi
                case "$subpick" in
                    edit)
                        local new_id new_status
                        new_id=$(ask     radiod.id          "$cur_id"     valid_radiod_id) || continue
                        new_status=$(pick_radiod_status "$cur_status") || continue
                        radiod_block_update "$pick" "$new_id" "$new_status"
                        ;;
                    delete)
                        if ask_yesno "Confirm delete" \
                                     "Remove [[radiod]] block id='$cur_id' status='$cur_status'?

This also drops the [radiod.ft8] and [radiod.ft4] sub-tables (freqs_hz lists) for this block.

The change isn't written until you choose Apply from the main menu."; then
                            radiod_block_delete "$pick"
                        fi
                        ;;
                    back) : ;;
                esac
                ;;
        esac
    done
}

collect_timing() {
    local chain_delay
    chain_delay=$(current_value timing chain_delay_ns)
    [[ -z "$chain_delay" ]] && chain_delay="0"
    chain_delay=$(ask timing.chain_delay_ns "$chain_delay" valid_nonneg_int) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('timing', {}).update({
    'chain_delay_ns': int('$chain_delay'),
})
print(json.dumps(d))
")
}

# Pick which radiod_id's env file to operate on.
# Returns the chosen id on stdout, or returns 1 on cancel.
pick_radiod_id_for_env() {
    local blocks_json
    blocks_json=$(radiod_list_json)
    local count
    count=$(BLOCKS="$blocks_json" python3 <<'PYEOF' 2>/dev/null
import json, os
print(len(json.loads(os.environ.get('BLOCKS') or '[]')))
PYEOF
)
    if [[ "$count" == "0" ]]; then
        whiptail --title "No radiod blocks" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "There are no [[radiod]] blocks defined yet.  Add one via the 'Radiod' menu item first, then come back here." \
                 12 "$WIDTH"
        return 1
    fi
    if [[ "$count" == "1" ]]; then
        # Single radiod -- no need to prompt.
        BLOCKS="$blocks_json" python3 <<'PYEOF' 2>/dev/null
import json, os
print(json.loads(os.environ.get('BLOCKS') or '[]')[0].get('id', ''))
PYEOF
        return 0
    fi
    # Multi-radiod: present a picker via a bash array (no eval).
    local -a menu_items=()
    local i id status
    for ((i = 0; i < count; i++)); do
        id=$(BLOCKS="$blocks_json" IDX="$i" python3 <<'PYEOF' 2>/dev/null
import json, os
print(json.loads(os.environ.get('BLOCKS') or '[]')[int(os.environ['IDX'])].get('id', ''))
PYEOF
)
        status=$(BLOCKS="$blocks_json" IDX="$i" python3 <<'PYEOF' 2>/dev/null
import json, os
print(json.loads(os.environ.get('BLOCKS') or '[]')[int(os.environ['IDX'])].get('radiod_status', ''))
PYEOF
)
        menu_items+=("$id" "status=$status")
    done
    whiptail --title "Pick instance for env" \
             --backtitle "$BACKTITLE" \
             --menu "Which [[radiod]] instance do you want to edit /etc/meteor-scatter/env/<id>.env for?" \
             "$HEIGHT" "$WIDTH" "$LIST_HEIGHT" \
             "${menu_items[@]}" 3>&1 1>&2 2>&3 || return 1
}

# Summary of pipelines for the first radiod_id's env file, or "(unset)".
delivery_summary() {
    local first_id
    first_id=$(radiod_ids_summary | cut -d, -f1)
    [[ -z "$first_id" || "$first_id" == "(none)" ]] && { echo "(no radiod blocks)"; return; }
    local env_file pipelines
    env_file=$("$METEOR_SCATTER" env show --json --instance "$first_id" 2>/dev/null)
    pipelines=$(ENV_JSON="${env_file:-{}}" python3 <<'PYEOF' 2>/dev/null
import json, os
d = json.loads(os.environ.get('ENV_JSON') or '{}')
print(d.get('METEOR_SCATTER_DELIVERY_PIPELINES') or d.get('METEOR_SCATTER_DELIVERY_MODE') or '(unset)')
PYEOF
)
    echo "${pipelines:-(unset)}"
}

# Read one key from /etc/sigmond/coordination.env (read-only display).
sigmond_coord_get() {
    local key="$1"
    [[ -r /etc/sigmond/coordination.env ]] || return 0
    sed -nE "s|^[[:space:]]*${key}=([\"']?)([^\"']*)\\1[[:space:]]*\$|\\2|p" \
        /etc/sigmond/coordination.env | tail -1
}

collect_delivery() {
    # Make sure there's a radiod block (env file is per-instance).
    local instance
    instance=$(pick_radiod_id_for_env) || return 1
    [[ -z "$instance" ]] && return 1

    # Show sigmond-managed context so the operator knows what's
    # coming from coordination.env vs the per-instance env file.
    local sqlite_path
    sqlite_path=$(sigmond_coord_get SIGMOND_SQLITE_PATH)
    [[ -z "$sqlite_path" ]] && sqlite_path="(unset -- sigmond.hamsci_sink will fall back to /var/lib/sigmond/sink.db)"
    whiptail --title "Sigmond-managed context (read-only)" \
             --backtitle "$BACKTITLE" \
             --msgbox "These come from /etc/sigmond/coordination.env (sigmond owns that file; the wizard never writes there):

  Local SQLite sink:  $sqlite_path
  STATION_CALL:       $(sigmond_coord_get STATION_CALL)
  STATION_GRID:       $(sigmond_coord_get STATION_GRID)

The next screens edit /etc/meteor-scatter/env/$instance.env -- the per-instance upload-destination knobs meteor-scatter reads at start (via systemd EnvironmentFile=)." \
             16 "$WIDTH"

    # Read current env file via `meteor-scatter env show`.
    local current_env
    current_env=$("$METEOR_SCATTER" env show --json --instance "$instance" 2>/dev/null)
    local cur_pipelines cur_hs_upload cur_dedup
    cur_pipelines=$(ENV_JSON="${current_env:-{}}" python3 <<'PYEOF' 2>/dev/null
import json, os
d = json.loads(os.environ.get('ENV_JSON') or '{}')
print(d.get('METEOR_SCATTER_DELIVERY_PIPELINES', d.get('METEOR_SCATTER_DELIVERY_MODE', 'direct')))
PYEOF
)
    cur_hs_upload=$(ENV_JSON="${current_env:-{}}" python3 <<'PYEOF' 2>/dev/null
import json, os
d = json.loads(os.environ.get('ENV_JSON') or '{}')
print(d.get('METEOR_SCATTER_USE_HS_UPLOADER', '1'))
PYEOF
)
    cur_dedup=$(ENV_JSON="${current_env:-{}}" python3 <<'PYEOF' 2>/dev/null
import json, os
d = json.loads(os.environ.get('ENV_JSON') or '{}')
print(d.get('METEOR_SCATTER_DIRECT_DEDUP', '0'))
PYEOF
)

    # METEOR_SCATTER_DELIVERY_PIPELINES: checklist of three options.
    local p_direct p_merge p_raw
    p_direct=$([[ ",$cur_pipelines," == *",direct,"*       ]] && echo "on" || echo "off")
    p_merge=$([[  ",$cur_pipelines," == *",server-merge,"* ]] && echo "on" || echo "off")
    p_raw=$([[    ",$cur_pipelines," == *",server-raw,"*   ]] && echo "on" || echo "off")
    local pick
    if ! pick=$(whiptail --title "$(help_get env.METEOR_SCATTER_DELIVERY_PIPELINES title)" \
                         --backtitle "$BACKTITLE" \
                         --separate-output \
                         --checklist "$(help_get env.METEOR_SCATTER_DELIVERY_PIPELINES help)" \
                         "$HEIGHT" "$WIDTH" 3 \
                         "direct"       "POST direct to pskreporter.info"             "$p_direct" \
                         "server-merge" "ship to wsprdaemon, server forwards one"     "$p_merge" \
                         "server-raw"   "ship to wsprdaemon, suppress server forward" "$p_raw" \
                         3>&1 1>&2 2>&3); then
        return 1
    fi
    # Auto-downgrade: if both 'direct' and 'server-merge' selected,
    # server-merge -> server-raw (per recorder.py).
    local new_pipelines
    new_pipelines=$(PICK="$pick" python3 <<'PYEOF' 2>/dev/null
import os
selected = set(os.environ.get('PICK', '').split())
if 'direct' in selected and 'server-merge' in selected:
    selected.discard('server-merge')
    selected.add('server-raw')
if not selected:
    selected = {'direct'}    # default fallback
print(','.join(sorted(selected)))
PYEOF
)

    # Booleans as yesno.
    local new_hs new_dedup
    if ask_yesno "$(help_get env.METEOR_SCATTER_USE_HS_UPLOADER title)" \
                 "$(help_get env.METEOR_SCATTER_USE_HS_UPLOADER help)"$'\n\n'"Current: $cur_hs_upload"; then
        new_hs=1
    else
        new_hs=0
    fi
    if ask_yesno "$(help_get env.METEOR_SCATTER_DIRECT_DEDUP title)" \
                 "$(help_get env.METEOR_SCATTER_DIRECT_DEDUP help)"$'\n\n'"Current: $cur_dedup"; then
        new_dedup=1
    else
        new_dedup=0
    fi

    # Write immediately via `meteor-scatter env apply` -- env files are
    # cheap to round-trip; no need to defer until the main Apply step.
    local payload
    payload=$(NEW_PIPES="$new_pipelines" NEW_HS="$new_hs" NEW_DEDUP="$new_dedup" \
              python3 <<'PYEOF' 2>/dev/null
import json, os
print(json.dumps({
    'METEOR_SCATTER_DELIVERY_PIPELINES': os.environ['NEW_PIPES'],
    'METEOR_SCATTER_USE_HS_UPLOADER':    os.environ['NEW_HS'],
    'METEOR_SCATTER_DIRECT_DEDUP':       os.environ['NEW_DEDUP'],
}))
PYEOF
)
    if ! printf '%s' "$payload" | \
            "$METEOR_SCATTER" env apply --json - --instance "$instance"; then
        whiptail --title "env apply failed" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "Couldn't write /etc/meteor-scatter/env/$instance.env.  See stderr for details.  Existing env file unchanged." \
                 12 "$WIDTH"
        return 1
    fi
    whiptail --title "Delivery written" \
             --backtitle "$BACKTITLE" \
             --msgbox "Wrote /etc/meteor-scatter/env/$instance.env:

  METEOR_SCATTER_DELIVERY_PIPELINES = $new_pipelines
  METEOR_SCATTER_USE_HS_UPLOADER    = $new_hs
  METEOR_SCATTER_DIRECT_DEDUP       = $new_dedup

Restart 'meteor-scatter@$instance.service' for the change to take effect." \
             14 "$WIDTH"
}

edit_raw_toml() {
    # Resolve target file path (same logic configurator.py uses).
    local target="${CONFIG_PATH:-/etc/meteor-scatter/meteor-scatter-config.toml}"
    if [[ ! -f "$target" ]]; then
        whiptail --title "Config file not found" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "No file at $target.  Apply any pending changes first, then re-enter this option." \
                 12 "$WIDTH"
        return 0
    fi

    # Apply any pending wizard changes first so $EDITOR sees a consistent file.
    # Three explicit options here (the old yesno had only Yes/No and
    # both opened the file -- the operator could not back out without
    # editing, and "No" was easy to mis-read as "no, don't open at all").
    if [[ "$SCRATCH_JSON" != "{}" ]]; then
        local pre_pick
        if ! pre_pick=$(whiptail \
                --title "Pending wizard edits" \
                --backtitle "$BACKTITLE" \
                --menu "You have pending wizard edits (Station / Paths / Processing / Timing / Radiod).

What should I do before opening $target in \$EDITOR?" \
                "$HEIGHT" "$WIDTH" 3 \
                "apply-then-open" "write pending edits, then open the file" \
                "discard-then-open" "discard pending edits, open the file as-is" \
                "back"              "(cancel: don't open the file at all)" \
                3>&1 1>&2 2>&3); then
            return 0   # Esc/Cancel = back; no open, no write
        fi
        case "$pre_pick" in
            back)
                return 0
                ;;
            discard-then-open)
                SCRATCH_JSON='{}'
                ;;
            apply-then-open)
                if ! printf '%s' "$SCRATCH_JSON" | \
                        "$METEOR_SCATTER" config apply --json - ${CONFIG_PATH:+--config "$CONFIG_PATH"}; then
                    whiptail --title "Apply failed" \
                             --backtitle "$BACKTITLE" \
                             --msgbox "Couldn't write pending edits.  Aborting open." \
                             12 "$WIDTH"
                    return 1
                fi
                SCRATCH_JSON='{}'
                ;;
        esac
    fi

    local editor="${EDITOR:-${VISUAL:-nano}}"
    if ! command -v "$editor" >/dev/null 2>&1; then
        editor=$(command -v nano 2>/dev/null || command -v vi)
        [[ -z "$editor" ]] && {
            whiptail --title "No editor found" \
                     --backtitle "$BACKTITLE" \
                     --msgbox "No \$EDITOR / \$VISUAL / nano / vi found on PATH.  Hand-edit $target with whatever you have." \
                     12 "$WIDTH"
            return 0
        }
    fi
    # Drop out of the whiptail UI for the editor session.
    clear
    "$editor" "$target"
    # Re-validate via meteor-scatter validate after the edit.
    local validate_rc=0
    "$METEOR_SCATTER" validate --json ${CONFIG_PATH:+--config "$CONFIG_PATH"} >/dev/null 2>&1 \
        || validate_rc=$?
    if [[ $validate_rc -ne 0 ]]; then
        whiptail --title "Validation warnings" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "meteor-scatter validate reported issues after your edit.  Run

    meteor-scatter validate --json | jq

to see the details.  The file was written as you saved it -- this is just a heads-up." \
                 14 "$WIDTH"
    fi
}

main_menu_loop() {
    # Display normalization: leftover template placeholders rendered as "(unset)".
    display() {
        local v="$1"
        if [[ -z "$v" || "$v" =~ ^\<.*\>$ ]]; then
            echo "(unset)"
        else
            echo "$v"
        fi
    }

    while :; do
        local cur_call cur_grid cur_spool cur_decoder cur_lifetime
        cur_call=$(display     "$(current_value station    callsign)")
        cur_grid=$(display     "$(current_value station    grid_square)")
        cur_spool=$(display    "$(current_value paths      spool_dir)")
        cur_decoder=$(display  "$(current_value paths      decoder)")
        cur_lifetime=$(current_value processing radiod_lifetime_frames)

        local cur_timing cur_radiods cur_pipelines
        cur_timing=$(current_value timing chain_delay_ns)
        # Radiod summary: comma-separated list of ids.
        cur_radiods=$(radiod_ids_summary)
        # Delivery summary: METEOR_SCATTER_DELIVERY_PIPELINES for the first radiod_id.
        cur_pipelines=$(delivery_summary)

        local choice
        choice=$(whiptail --title "meteor-scatter configuration" \
                          --backtitle "$BACKTITLE" \
                          --cancel-button "Exit wizard" \
                          --menu "Pick a section to edit, or Apply when you're done.

Each section's questions walk linearly; Cancel inside a section
drops back here instead of aborting." \
                          "$HEIGHT" "$WIDTH" 9 \
                          "Station"    "Call=$cur_call  Grid=$cur_grid" \
                          "Paths"      "spool=$cur_spool  decoder=$cur_decoder" \
                          "Processing" "lifetime=${cur_lifetime:-(unset)} frames" \
                          "Timing"     "chain_delay=${cur_timing:-0} ns (sigmond usually overrides)" \
                          "Radiod"     "blocks: $cur_radiods" \
                          "Delivery"   "pipelines: $cur_pipelines (per-instance env)" \
                          "Edit-TOML"  "Open raw config in \$EDITOR (for freqs_hz lists)" \
                          "Apply"      "Review and write changes" \
                          "Cancel"     "Discard pending changes and exit" \
                          3>&1 1>&2 2>&3)
        if [[ $? -ne 0 ]]; then
            if ask_yesno "Discard changes?" "Discard any pending changes and exit the wizard?"; then
                return 1
            fi
            continue
        fi

        case "$choice" in
            Station)    collect_station    || true ;;
            Paths)      collect_paths      || true ;;
            Processing) collect_processing || true ;;
            Timing)     collect_timing     || true ;;
            Radiod)     collect_radiod     || true ;;
            Delivery)   collect_delivery   || true ;;
            Edit-TOML)  edit_raw_toml      || true ;;
            Apply)
                if confirm_and_write; then
                    return 0
                fi
                ;;
            Cancel)
                if ask_yesno "Discard changes?" "Discard any pending changes and exit the wizard?"; then
                    return 1
                fi
                ;;
        esac
    done
}

confirm_and_write() {
    if [[ "$SCRATCH_JSON" == "{}" ]]; then
        whiptail --title "Nothing to apply" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "You haven't changed any of the wizard-managed sections (Station / Paths / Processing).

If you only used 'Edit raw TOML', your changes were written directly when you saved the file.  Exit the wizard to confirm." \
                 14 "$WIDTH"
        return 1
    fi

    local summary
    summary=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
lines = []
def walk(prefix, obj):
    for k, v in obj.items():
        if isinstance(v, dict):
            walk(prefix + k + '.', v)
        else:
            lines.append(f'{prefix}{k} = {v!r}')
walk('', d)
print('\n'.join(lines))
")
    if ! whiptail --title "Review and write" \
                  --backtitle "$BACKTITLE" \
                  --yesno "About to apply the following to ${CONFIG_PATH:-/etc/meteor-scatter/meteor-scatter-config.toml}:

$summary

Continue?" "$HEIGHT" "$WIDTH"; then
        return 1
    fi
    if ! printf '%s' "$SCRATCH_JSON" | \
            "$METEOR_SCATTER" config apply --json - ${CONFIG_PATH:+--config "$CONFIG_PATH"}; then
        whiptail --title "Apply failed" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "meteor-scatter config apply rejected the input.  See stderr for details.  Existing config was not modified." \
                 12 "$WIDTH"
        return 1
    fi
    whiptail --title "Config written" \
             --backtitle "$BACKTITLE" \
             --msgbox "Configuration written.

Next steps:
  - Verify:   meteor-scatter validate --json | jq
  - Restart:  sudo systemctl restart meteor-scatter@<radiod_id>.service

Note: [[radiod]] blocks and freqs_hz weren't touched by this wizard -- if you need to change those, re-enter the wizard and pick 'Edit raw TOML'." \
             "$HEIGHT" "$WIDTH"
}

# -------- main flow --------------------------------------------------

SCRATCH_JSON='{}'

welcome_screen || { echo "wizard: cancelled at welcome" >&2; exit 1; }
if main_menu_loop; then
    exit 0
else
    echo "wizard: exited without writing" >&2
    exit 1
fi
