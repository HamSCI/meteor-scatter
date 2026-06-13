# Operations guide

Running msk144-recorder day-to-day: starting/stopping, reading logs,
verifying upload health, troubleshooting.

## Service control

```bash
sudo systemctl start    msk144-recorder@<radiod_id>
sudo systemctl stop     msk144-recorder@<radiod_id>
sudo systemctl restart  msk144-recorder@<radiod_id>
sudo systemctl status   msk144-recorder@<radiod_id>

# All instances at once:
sudo systemctl restart 'msk144-recorder@*'
```

The unit is `Type=notify` with `WatchdogSec=120`. The daemon sends
`READY=1` once it has provisioned channels and `WATCHDOG=1` every ~30s
thereafter. If the main loop stalls > 120s, systemd kills and restarts
it.

After repeated start failures, systemd will give up
(`StartLimitBurst=10` over `StartLimitIntervalSec=300`). Reset with:

```bash
sudo systemctl reset-failed msk144-recorder@<radiod_id>
```

## Logs

Three log streams per instance:

| Stream | Written by | Contents |
|---|---|---|
| systemd journal (`SyslogIdentifier=msk144-recorder@<id>`) | the daemon's stdout (`StandardOutput=journal`) | Process log: startup, channel provisioning, slot stats, errors. |
| `/var/log/msk144-recorder/<id>-ft8.log` | the decoder (forked per slot) | One line per FT8 spot. Tailed by `pskreporter-sender`. |
| `/var/log/msk144-recorder/<id>-ft4.log` | the decoder (forked per slot) | One line per FT4 spot. Tailed by `pskreporter-sender`. |

`journalctl -u msk144-recorder@<id>` (or `smd log msk144-recorder`) shows the
process log; the per-mode spot logs stay as files. Only the file-based
spot logs are listed in `inventory --json` `log_paths`.

### Per-minute stats lines

The recorder emits a line per mode every 60s:

```
INFO:msk144_recorder.core.recorder:stats FT8: spots=10 decodes=44/44 slots_empty=0 freqs=11 (60s window)
```

| Field | Meaning |
|---|---|
| `spots` | New spots written to the spot log this window. |
| `decodes=N/M` | N successful `decode_ft8` exits out of M invocations. |
| `slots_empty` | Slots where the ring buffer didn't have enough samples — usually startup transient or a dropped/restored stream. |
| `freqs` | Number of channels active for this mode. |

A healthy FT8 instance typically shows `decodes=freqs×4` (60s ÷ 15s)
and a non-zero `spots` value during reasonable propagation. FT4 has
half the slots per minute (`freqs×8`) and lower spot density.

## Validating and inventorying

```bash
msk144-recorder validate --json
msk144-recorder inventory --json
msk144-recorder version --json
```

These commands keep stdout clean for piping into `jq`. All app logging
goes to stderr.

`validate` reports config errors and warnings. Exit code 0 if no
`severity: fail`.

`inventory` returns the full per-instance resource view that sigmond
uses for cross-client coordination. See
[SIGMOND-CONTRACT.md §3](SIGMOND-CONTRACT.md).

## Verifying uploads

### Locally

```bash
ps -ef | grep pskreporter-sender    # one per (radiod_id, mode)
sudo ss -tnp | grep 4739            # active TCP conn (only during upload window)
sudo journalctl -u msk144-recorder@<id> -f | grep -E 'tcp_upload|uploading'
```

`pskreporter-sender` connects to `report.pskreporter.info:4739` only
when it has a batch to send (~3 min jitter). With TCP, the connection
is reopened each upload cycle (a deliberate workaround for
half-closed-socket loss; see commit history of
[ftlib-pskreporter](https://github.com/pjsg/ftlib-pskreporter)).

### Remotely

PSK Reporter's retrieve API:

```
https://www.pskreporter.info/cgi-bin/pskquery5.pl?senderCallsign=<callsign>&flowStartSeconds=-3600
```

Look for spots with your callsign as `senderCallsign` (you uploaded
them) within the last hour.

## Common failure modes

### "Failed to resolve `<host>-status.local`"

Avahi can't see the radiod. Either `radiod@<id>.service` is not
running on the LAN, or mDNS is broken. Check:

```bash
systemctl is-active radiod@<id>
avahi-resolve -n <host>-status.local
```

### "insufficient samples, skipping" (sustained, not just startup)

The ring buffer doesn't have a full slot of audio when the slot
worker fires. Usually means the RTP stream dropped. Check the journal
for `on_stream_dropped` / `on_stream_restored` events and verify
network multicast is reaching the host. Brief bursts of these
warnings on startup are normal.

### `decodes=N/N spots=0` for many minutes on one mode (esp. FT4)

Decoder is running but finding no signals. Could be:
- Bands genuinely quiet for that mode (FT4 is sparser than FT8).
- Wrong audio level — check that other modes on the same radiod
  produce spots.
- Slot timing off — set `keep_wav = true` (see [CONFIG.md](CONFIG.md)),
  catch a few WAVs, run `decode_ft8 -4 -f <mhz> <wav>` by hand and
  verify in a hex editor that you have a full 7.5 s of audio.

### "Dropping N spots as too old (without connectivity)"

`pskreporter-sender` ignores spots older than ~50 minutes. On
startup it tails the existing spot log and may pick up entries from
prior days. This is benign — only worry if it happens to fresh
spots, which would indicate the upload pipe is broken (check network,
then `pskreporter_tcp` setting).

### Service in `failed` state with `result 'protocol'`

systemd `Type=notify` saw the daemon exit before sending `READY=1`.
The app log will have the actual error — usually mDNS resolution
failure (above) or a config validation fault.

### `pskreporter-sender` exits and restarts in a loop

The supervisor in `uploader.py` restarts on exit with backoff. Look
in the journal (`journalctl -u msk144-recorder@<id>`) for
`[pskreporter-ft8]` / `[pskreporter-ft4]` stderr lines — the sender's argparse output
appears there, and any Python tracebacks. A common cause is the
sender importing the wrong `pskreporter` Python module if the
configured binary uses `#!/usr/bin/env python3` outside the venv.

## Debugging WAVs — the decoder shim pattern

`keep_wav = true` by itself is not enough. `decode_ft8` unconditionally
unlinks the WAV it just decoded (contract §12.4), so the spool dir
stays empty no matter what the config says. To capture WAVs for
inspection, install a shim decoder that copies the file aside before
exec'ing the real one. Put it under a path in the unit's
`ReadWritePaths`:

```bash
sudo mkdir -p /var/lib/msk144-recorder/debug
sudo chown msk144rec:msk144rec /var/lib/msk144-recorder/debug
sudo tee /usr/local/bin/decode_ft8-shim >/dev/null <<'SH'
#!/bin/bash
set -e
wav="${@: -1}"
mode=ft8
for arg in "$@"; do [[ "$arg" == "-4" ]] && mode=ft4; done
cp -p "$wav" "/var/lib/msk144-recorder/debug/${mode}_$(basename $wav)" 2>/dev/null || true
exec /usr/local/bin/decode_ft8 "$@"
SH
sudo chmod 0755 /usr/local/bin/decode_ft8-shim
```

Point the config at the shim and restart:

```bash
sudo sed -i 's|^decoder.*|decoder     = "/usr/local/bin/decode_ft8-shim"|' \
    /etc/msk144-recorder/msk144-recorder-config.toml
sudo systemctl restart msk144-recorder@<radiod_id>
```

Then inspect:

```bash
ls /var/lib/msk144-recorder/debug/ | head
python3 -c "
import wave, struct, math
w = wave.open('/var/lib/msk144-recorder/debug/ft4_XXXXXX_14080.wav')
n = w.getnframes(); raw = w.readframes(n)
s = struct.unpack(f'<{len(raw)//2}h', raw)
print(f'frames={n} rate={w.getframerate()} '
      f'peak={max(abs(x) for x in s)} '
      f'rms={math.sqrt(sum(x*x for x in s)/n):.1f}')
"
/usr/local/bin/decode_ft8 -4 -f 14.080000 /var/lib/msk144-recorder/debug/ft4_*_14080.wav
```

Clean up when done: point `decoder` back at `/usr/local/bin/decode_ft8`,
restart, `rm -rf /var/lib/msk144-recorder/debug /usr/local/bin/decode_ft8-shim`.
The shim dir fills fast — a full FT8+FT4 install across many bands is
on the order of GB/hour.

This pattern is how the April 2026 "silent FT4" investigation caught
the low-amplitude bug ([src/msk144_recorder/core/wav.py](../src/msk144_recorder/core/wav.py)
RMS-target normalization): radiod was delivering real audio at
`peak ≈ 30` out of an int16 range of 32767, below what `decode_ft8`
could reliably find FT4 signals in.
