#!/usr/bin/env python3
"""Compare two meteor-scatter decode logs slot by slot (the F32 side-by-side, plan Task 4).

    compare-capture.py OLD_LOG NEW_LOG [--from "YYYY/MM/DD HH:MM"] [--to "YYYY/MM/DD HH:MM"]

Each log is a <radiod>-msk144.log: "YYYY/MM/DD HH:MM:SS <snr> <dt> <freq_hz> & <message>".
A slot is (slot start, band); a band is the dial's MHz.  Within a slot a message counts once,
at its best SNR, since one meteor can give several pings of the same message.  Prints a row
for every message, with both SNRs and the difference where both sides heard it, and totals.
Exit 0 after a comparison: the verdict is for a person to read.  Exit 2 for bad
arguments or a log it cannot read.
"""
import argparse
import re
import statistics
import sys
from collections import defaultdict

LINE = re.compile(
    r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\s+(-?\d+)\s+([+-]?\d+\.\d+)\s+(\d+)\s+&\s+(.*?)\s*$")


def load(path, lo=None, hi=None):
    """{(slot, band): {message: best_snr}} for the lines between lo and hi (inclusive)."""
    out = defaultdict(dict)
    with open(path) as f:
        for line in f:
            m = LINE.match(line)
            if not m:
                continue
            ts, snr, _dt, freq, msg = m.groups()
            if (lo and ts < lo) or (hi and ts > hi):
                continue
            key = (ts, int(freq) // 1_000_000)
            snr = int(snr)
            if msg not in out[key] or snr > out[key][msg]:
                out[key][msg] = snr
    return out


def compare(old, new):
    rows, deltas = [], []
    tally = {"slots_both": 0, "slots_old_only": 0, "slots_new_only": 0,
             "msgs_both": 0, "msgs_old_only": 0, "msgs_new_only": 0}
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key, {}), new.get(key, {})
        side = "both" if o and n else ("old_only" if o else "new_only")
        tally["slots_" + side] += 1
        for msg in sorted(set(o) | set(n)):
            if msg in o and msg in n:
                tally["msgs_both"] += 1
                deltas.append((key, msg, n[msg] - o[msg]))
                rows.append((key, "both", msg, (o[msg], n[msg])))
            elif msg in o:
                tally["msgs_old_only"] += 1
                rows.append((key, "old only", msg, o[msg]))
            else:
                tally["msgs_new_only"] += 1
                rows.append((key, "new only", msg, n[msg]))
    return tally, deltas, rows


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="compare-capture.py",
        description="Compare two meteor-scatter decode logs slot by slot.")
    p.add_argument("old_log", metavar="OLD_LOG")
    p.add_argument("new_log", metavar="NEW_LOG")
    p.add_argument("--from", dest="lo", metavar='"YYYY/MM/DD HH:MM"',
                   help="first slot to count (inclusive)")
    p.add_argument("--to", dest="hi", metavar='"YYYY/MM/DD HH:MM"',
                   help="last minute to count (inclusive: HH:MM runs to HH:MM:59)")
    return p.parse_args(argv)


def main(argv):
    try:
        a = parse_args(argv)
    except SystemExit as e:          # argparse has printed the usage and the reason
        return e.code
    lo, hi = a.lo, a.hi
    if hi and len(hi) == 16:
        hi += ":59"
    try:
        old, new = load(a.old_log, lo, hi), load(a.new_log, lo, hi)
    except OSError as e:
        print(f"compare-capture.py: cannot read {e.filename}: {e.strerror}", file=sys.stderr)
        return 2
    tally, deltas, rows = compare(old, new)
    deltas = [d for _key, _msg, d in deltas]
    for (ts, band), side, msg, snr in rows:
        if side == "both":
            old_snr, new_snr = snr
            print(f"{ts}  {band:3d} MHz  {side:8s}  old={old_snr:+d} new={new_snr:+d} "
                  f"d={new_snr - old_snr:+d}  {msg}")
        else:
            print(f"{ts}  {band:3d} MHz  {side:8s}  {snr:+3d} dB  {msg}")
    print("")
    print(f"slots: both {tally['slots_both']}, old only {tally['slots_old_only']}, "
          f"new only {tally['slots_new_only']}")
    print(f"messages: both {tally['msgs_both']}, old only {tally['msgs_old_only']}, "
          f"new only {tally['msgs_new_only']}")
    if deltas:
        print(f"SNR new - old on messages both heard: median {statistics.median(deltas):+.1f} dB, "
              f"mean {statistics.mean(deltas):+.2f}, worse in {sum(d < 0 for d in deltas)}, "
              f"equal in {sum(d == 0 for d in deltas)}, better in {sum(d > 0 for d in deltas)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
