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
#   e.g. remove-stray-instance-dirs.sh /var/lib/meteor-scatter
# Always exits 0: a leftover directory must never fail an install.
set -u
root="${1:?usage: $0 <state-root>}"
[ -d "$root" ] || exit 0
shopt -s nullglob
for d in "$root"/*'\x'[0-9a-fA-F][0-9a-fA-F]*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    if [ -z "$(find "$d" -mindepth 1 ! -type d -print -quit)" ] \
       && [ -z "$(find "$d" -mindepth 2 -print -quit)" ]; then
        rm -r -- "$d" && echo "[INFO]  removed empty stray directory $d"
    else
        echo "[WARN]  left $d in place: it holds more than empty mode directories:" >&2
        find "$d" -mindepth 1 | head -20 >&2
    fi
done
exit 0
