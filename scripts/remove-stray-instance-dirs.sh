#!/bin/bash
# remove-stray-instance-dirs.sh — remove the per-instance directories an
# older unit template left under the state root.
#
# Before v3.72 the unit ran `mkdir -p <state-root>/%i/<mode>`, and %i keeps
# systemd's spelling, so a reporter-keyed instance (AC0G=B4) got a directory
# named AC0G\x3dB4 that nothing reads: the spool keys on the radiod id.
# This removes such a directory only when it holds nothing but empty
# directories one level down (the mode directories the template made).
# Anything else stays, and the script names what it found.
#
# Usage: remove-stray-instance-dirs.sh <state-root>
#   e.g. remove-stray-instance-dirs.sh /var/lib/<client>
# Always exits 0: a leftover directory must never fail an install.
set -u
root="${1:?usage: $0 <state-root>}"
[ -d "$root" ] || exit 0
shopt -s nullglob
left_in_place() {
    echo "[WARN]  left $1 in place: it holds more than empty mode directories:" >&2
    find "$1" -mindepth 1 2>/dev/null | head -20 >&2
}
for d in "$root"/*'\x'[0-9a-fA-F][0-9a-fA-F]*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    # Pre-filter: a find that fails (unreadable subdirectory) counts as
    # "not empty", so a failure can only keep a directory, never remove one.
    files=$(find "$d" -mindepth 1 ! -type d -print -quit) || { left_in_place "$d"; continue; }
    deep=$(find "$d" -mindepth 2 -print -quit) || { left_in_place "$d"; continue; }
    if [ -n "$files" ] || [ -n "$deep" ]; then
        left_in_place "$d"
        continue
    fi
    # rmdir only: it refuses any directory that holds something, so content
    # that appears after the check above survives.  Never rm -r.
    ok=1
    while IFS= read -r -d '' sub; do
        rmdir -- "$sub" 2>/dev/null || ok=0
    done < <(find "$d" -mindepth 1 -maxdepth 1 -type d -print0)
    if [ "$ok" = 1 ] && rmdir -- "$d" 2>/dev/null; then
        echo "[INFO]  removed empty stray directory $d"
    else
        left_in_place "$d"
    fi
done
exit 0
