# Operations guide

> **Audience:** operator/contributor
> **Status:** current
> **Verified against:** meteor-scatter bac2116 on 2026-08-23 — code
> **Canonical for:** running meteor-scatter day-to-day — control, logs, health, failures

Running meteor-scatter day-to-day: starting and stopping, reading logs,
telling healthy from broken, and what a restart actually costs.

**Set your expectations first.** Meteor pings are *rare*. A healthy
instance can sit at `spots=0` for hours and still be perfectly well —
this client's normal state is a lot of empty slots. Every health signal
below is therefore built on *slot progress*, never on spot count.

## Service control

```bash
sudo systemctl start    meteor-scatter@<instance>
sudo systemctl stop     meteor-scatter@<instance>
sudo systemctl restart  meteor-scatter@<instance>
sudo systemctl status   meteor-scatter@<instance>

# All instances at once:
sudo systemctl restart 'meteor-scatter@*'
```

Under sigmond, prefer `smd start meteor-scatter` / `smd restart` — see
the restart-cost warning below before you reach for either.

The unit is `Type=notify` with `WatchdogSec=120`. The daemon sends
`READY=1` once channels are provisioned and `WATCHDOG=1` while the
record→decode pipeline keeps advancing; a stall of 90 s withholds the
ping and systemd restarts it at 120 s. `TimeoutStartSec=180` is generous
for the two-channel layout because each channel is a setpolicy
round-trip with radiod.

The instance name is escaped by systemd but not by the config path — a
reporter id like `AC0G=S` is `meteor-scatter@AC0G\x3dS.service` as a unit
and `/etc/meteor-scatter/AC0G=S.toml` as a file. `smd watch meteor
--instance AC0G=S` handles the translation for you.

After repeated start failures systemd gives up
(`StartLimitBurst=10` / `StartLimitIntervalSec=300`):

```bash
sudo systemctl reset-failed meteor-scatter@<instance>
```

One exit is special: **78** (`EX_CONFIG`) means the daemon found the
`<configure-via-config-init>` placeholder as a radiod status. The unit's
`RestartPreventExitStatus=78` stops it cleanly rather than crash-looping
a config that can never succeed. Fix it with `meteor-scatter config init`.

## ⛔ Restarting meteor-scatter bounces the radio

meteor-scatter declares `requires = ["ka9q-python", "ka9q-radio"]` in
sigmond's catalog, and `smd restart` expands that requires-closure. So
`smd restart meteor-scatter` restarts **radiod for the whole station** —
every other recorder loses its RTP anchor, and hf-timestd's timing
products need roughly ten minutes to re-settle. See sigmond's
[troubleshooting §"what a restart actually touches"](https://github.com/HamSCI/sigmond/blob/main/docs/operator/troubleshooting.md)
for the full table and the narrow-action guidance.

Since the radio is going to bounce anyway, bounce it **once**: prefer
`smd restart all` (one clean bounce, everything re-anchored together)
over restarting one name and leaving the rest stale. A plain
`sudo systemctl restart meteor-scatter@<instance>` does *not* expand the
closure and is the genuinely narrow action when you only want this
daemon back — the cost is that it re-anchors only itself.

## Logs

Two log streams per instance:

| Stream | Written by | Contents |
|---|---|---|
| systemd journal (`SyslogIdentifier=meteor-scatter@<instance>`) | the daemon's stdout (`StandardOutput=journal`) | Process log: settle gate, channel provisioning, per-cycle commits, 60 s stats, errors |
| `/var/log/meteor-scatter/<radiod_id>-msk144.log` | `SlotWorker` (normalized from jt9's `decoded.txt`) | One line per decoded ping. Read by `ChTailer` and by `smd watch meteor` |

```bash
journalctl -fu meteor-scatter@<instance>
smd log meteor-scatter
tail -f /var/log/meteor-scatter/<radiod_id>-msk144.log
```

Only the file-based spot log is listed in `inventory --json`'s
`log_paths` (the journal is not a file sink). The log key is the
**radiod status name**, not the systemd instance name — on a
reporter-keyed instance the two differ.

The spot-log line shape (`core.decoder.normalize_log_line`):

```
YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>
```

`&` is MSK144's sync indicator — the same field layout psk-recorder's
logs use, where the separator is `~` (decode_ft8) instead. The
timestamp is the RTP-anchored slot boundary, not jt9's own time column.

### `smd watch meteor`

```bash
smd watch meteor                      # every receiver, summary counts
smd watch meteor 5                    # first 5 decodes per cycle
smd watch meteor -v                   # every decode
smd watch meteor --instance <rid>     # scope to one instance
```

It polls each receiver's `<radiod_id>-msk144.log` every 15 s plus a 4 s
settle and prints what landed since the last fire. It watches the
**decoder → per-mode log** stage: pre-SQLite, pre-upload. That is also
why `smd watch psk` does not show MSK144 — it tails psk-recorder's own
`-ft8.log` / `-ft4.log`, a different producer, even though both clients
end up in one `psk.spots` table.

For the upload-side audit — lost / in-flight / delivered / cadence — use
`smd admin verifier report --target psk`. That is the surface
`deploy.toml`'s `[client_features.verifier] verb = "psk"` registers, and
it covers MSK144 rows because they live in the same `psk.spots` queue.
(`smd watch verifier` is a different thing: it replays wspr-recorder's
verifier journal events.)

### The 60 s stats line

```
INFO:meteor_scatter.core.recorder:stats MSK144 rx=<radiod_id>: spots=0 decodes=4/4 slots_empty=0 freqs=2 (60s window)
```

| Field | Meaning |
|---|---|
| `spots` | New lines appended to that receiver's spot log this window. **Zero is normal.** |
| `decodes=N/M` | N clean `jt9` exits out of M invocations. |
| `slots_empty` | Slots where the ring did not hold enough samples — a startup transient, or a dropped/restored stream. |
| `freqs` | Channels active for this receiver. |

## Health signs

Read them in this order; each one rules out a layer.

1. **`decodes=N/M` with `N == M` and `M ≈ freqs × 60/tr_period_sec`.**
   At the default 30 s T/R period and two channels that is `4/4` per
   minute. This is the liveness signal — audio is arriving, slots are
   completing, jt9 is running and exiting cleanly. It is also exactly
   what the watchdog counts.
2. **`slots_empty` at 0** after the first minute. Sustained non-zero
   means RTP is not arriving reliably.
3. **`spots` occasionally non-zero** over hours, and rising during a
   meteor shower. Never treat a zero-spot hour as a fault on its own.
4. **A per-cycle commit line** from the batcher when spots do land, and
   a matching row count in the sink:
   ```bash
   sqlite3 /var/lib/sigmond/sink.db \
     "SELECT COUNT(*) FROM pending_uploads
       WHERE target_db='psk' AND target_table='spots'
         AND json_extract(payload_json,'\$.mode')='msk144';"
   ```
   (the sink is a queue table, `pending_uploads`, keyed by
   `target_db`/`target_table` — `psk.spots` is that pair, not a table
   name you can select from directly.)
5. **Delivery consistent with the mode you configured** — in `deposit`
   the journal says so at startup ("PSKReporter uploader disabled …
   deposit to the psk.spots sink only") and rows carry
   `forward_to_pskreporter=1`; in `direct` an `HsPskReporterUploader`
   line appears and it pumps every 30 s.

```bash
meteor-scatter validate --json | jq        # exit 0 and no severity:"fail"
meteor-scatter inventory --json | jq
meteor-scatter version --json
```

All three keep stdout clean for `jq`; logging goes to stderr.

⚠ `meteor-scatter status` is a **stub** in this build — it
unconditionally prints "not running (Phase 1 not yet implemented)" and
exits 2, whatever the daemon is doing. Ignore it; there is also no
contract §13 control socket yet. Use the journal, the stats line and
`smd watch meteor` instead (see
[SIGMOND-CONTRACT.md](SIGMOND-CONTRACT.md)).

## Common failure modes

### "Failed to resolve `<host>-status.local`"

Avahi cannot see the radiod. Either `radiod@<id>.service` is not running
on the LAN, or mDNS is broken:

```bash
systemctl is-active radiod@<id>
avahi-resolve -n <host>-status.local
```

### Service in `failed` state with `result 'protocol'`

`Type=notify` saw the daemon exit before `READY=1`. The journal carries
the real error — usually the mDNS failure above or a config validation
fault.

### Exits 78 immediately and stays stopped

The radiod `status` is still the `<configure-via-config-init>`
placeholder. That is the fail-fast working as designed; run
`meteor-scatter config init`.

### Channel verify warnings, then fewer `freqs` than configured

A cold or busy radiod is slow to confirm new channels. Each channel gets
10 s × 2 attempts inside a 120 s whole-provisioning budget, then is
skipped with a warning rather than killing the daemon. Raise
`METEOR_SCATTER_CHANNEL_VERIFY_TIMEOUT_S` / `_BUDGET_S` on a host where
radiod is habitually loaded, and check whether radiod itself is
overloaded by peer clients.

### Sustained `slots_empty`, or "insufficient samples"

The ring did not hold a full slot when the worker fired. Usually the RTP
stream dropped: look for `on_stream_dropped` / `on_stream_restored` in
the journal and verify multicast is reaching the host. Bursts at startup
are normal.

### `decodes=N/N` but the spot log never grows

Expected most of the time — see the expectations note at the top. Rule
out the boring causes first: is `tr_period_sec` the period the stations
you monitor actually transmit on (a mismatch decodes but smears `dt`
across adjacent slots)? Is the `usb` preset still full-width (a narrowed
filter clips MSK144's ~2.5 kHz and kills decodes outright)? Then keep a
few slots and try by hand:

```toml
[paths]
keep_wav = true
```

```bash
sudo systemctl restart meteor-scatter@<instance>
ls /var/lib/meteor-scatter/<radiod_id>/msk144/ | head
cd /tmp && touch plotspec decdata
jt9 -Y --msk144 -p 30 -f 1500 -a /tmp /var/lib/meteor-scatter/<radiod_id>/msk144/<slot>.wav
cat /tmp/decoded.txt
```

Note that jt9 prints only `<DecodeFinished> …` to stdout; the decodes
are in `decoded.txt` in the `-a` directory. Unlike `decode_ft8`, jt9
does **not** unlink the WAV it decoded, so `keep_wav = true` is
sufficient here — no shim decoder needed. Turn it back off when you are
done; the spool grows ~24 KB/s per channel.

### Rows in the sink but nothing at PSKReporter

Check the delivery mode first (`grep DELIVERY
/etc/meteor-scatter/env/<instance>.env`). In `deposit` this is correct
behaviour — the wsprdaemon server's forwarder owns that hop, and the
row's `forward_to_pskreporter=1` is what tells it so. In `direct`,
confirm the uploader started at all: it refuses (with a warning) when
`[station].callsign` or `grid_square` is empty.

### The direct pipeline stalls every 30 s with `disk I/O error`

`METEOR_SCATTER_DIRECT_DEDUP=1` on a host that shares `sink.db` with
wspr-recorder: the dedup CTE's materialisation trips
`sqlite3.OperationalError`. Set it back to `0` (the default). A
single-source host has nothing to dedup anyway.

### "attempt to write a readonly database" from any hs-uploader client

Something re-chowned `/var/lib/hs-uploader`. It is shared across client
service users and its `root:sigmond` 02775 shape belongs to
`tmpfiles.d/hs-uploader.conf`. Restore it; do not let any unit chown it.

### Slot timestamps look wrong after a host clock jump

The daemon anchors each channel once, after the chrony settle gate
passes. If `chronyc` was unavailable the journal says so loudly and the
anchor was taken unverified. Restart the instance after the clock is
disciplined — but read the restart-cost section first.

## Where the spots go

```
<radiod_id>-msk144.log  →  ChTailer  →  cycle batcher  →  psk.spots (mode="msk144")
                                                              │
                                     DELIVERY_MODE=direct ────┼──► pskreporter.info
                                     DELIVERY_MODE=deposit ───┘    (server forwarder)
```

The sink table is **`psk.spots`**, shared with psk-recorder's FT8/FT4
rows — MSK144 is a value of the per-row `mode` column, not a table of
its own. One consequence worth knowing: sigmond's `storage_trim`
retention policy keys on `("psk","spots")` as a whole, so MSK144 rows
are trimmed by the same policy as PSK rows.
