# Architecture

> **Audience:** contributor
> **Status:** current
> **Verified against:** meteor-scatter d29f733 on 2026-08-24 — code
> **Canonical for:** meteor-scatter internals — the MSK144 record → decode → sink pipeline

meteor-scatter monitors **meteor-scatter pings** on the conventional
WSJT-X **MSK144** dial frequencies, decodes each recorded T/R slot with
WSJT-X's `jt9 --msk144`, and stages the resulting spots into the shared
`psk.spots` sink. It is *not* an FT8/FT4 recorder — that is its sibling
[psk-recorder](https://github.com/HamSCI/psk-recorder). The two share a
skeleton, a sink table and a callhash table, and nothing else.

The high-level pipeline:

```
radiod (ka9q-radio)
  │  RadiodControl.ensure_channel() via ka9q-python
  │  preset=usb, samprate=12000, encoding=s16be
  ▼
RTP multicast ──► meteor-scatter daemon (one per radiod instance)
   │
   ├─ ReceiverManager (one per radiod / source)
   │    └─ MultiStream (grouped by multicast destination)
   │         └─ ChannelSink (one per configured MSK144 frequency)
   │              ├─ ka9q.SlotClock  — anchored ONCE off radiod's RTP timestamp
   │              ├─ Ring            — process-local deque behind a lock
   │              └─ SlotWorker      — harvests each completed T/R slot,
   │                                   writes a mono s16 12 kHz WAV, forks
   │                                   `jt9 -Y --msk144 -p <tr> -f 1500 -a <wd>`,
   │                                   reads the delta appended to decoded.txt,
   │                                   normalizes and appends to the mode log
   │
   ├─ ChTailer (one per radiod)
   │    tails /var/log/meteor-scatter/<radiod_id>-msk144.log,
   │    resolves compound-call hashes through the shared callhash table,
   │    stamps provenance (timing_authority, reporter_id, rx_source, …)
   │
   ├─ MeteorScatterCycleBatcher
   │    buckets rows by (mode, cycle) × source, owns the one SQLite
   │    connection, emits the per-cycle log line
   │
   └─ sigmond.hamsci_sink.Writer(table="spots", mode="psk", schema_version=2)
        → /var/lib/sigmond/sink.db   (psk.spots; per-row mode="msk144")
             │
             └─ HsPskReporterUploader (only when DELIVERY_MODE=direct)
                  hs-uploader Pipeline + PskReporterTcp → pskreporter.info
```

Two facts in that diagram are easy to get wrong and are load-bearing:

* **The sink target is `psk.spots`, not `msk144.spots`.** The writer is
  constructed `Writer.from_env(table="spots", mode="psk", …)` in
  `ch_tailer._default_writer_factory`; MSK144 is a *value of the per-row
  `mode` column* (`"msk144"`), not a namespace of its own. That is
  deliberate: the wsprdaemon server unifies `ft8` / `ft4` / `msk144` in
  one `psk.spots` table behind one PSKReporter forwarder, so one
  pipeline (`mode IN (ft8, ft4, msk144)`) delivers all three and the
  cycle-tar path carries MSK144 for free.
* **The default T/R period is 30 s, not 15 s.** `MSK144_CADENCE_SEC =
  30.0` (`config.py`) and `MSK144_TR_PERIOD_SEC = 30` (`core/decoder.py`)
  match a stock WSJT-X MSK144 install, which transmits on a 30 s
  sequence. `[radiod.msk144].tr_period_sec` overrides it per radiod
  (WSJT-X also offers 5/10/15 s; 15 s is the 2 m VHF meteor-ping
  convention). The recorded slot, the `jt9 -p` argument and the decode
  cadence are all the same number.

## Source layout

```
src/meteor_scatter/
  cli.py              # argparse entry point + stdout-cleanliness guard
  config.py           # TOML loader, [[radiod]] block resolution, defaults,
                      #   MSK144_CADENCE_SEC, MODES = ("msk144",)
  contract.py         # inventory/validate JSON builders (CONTRACT_VERSION = "0.8")
  configurator.py     # `config init|edit|show|apply`, `env show|apply`
  version.py          # GIT_INFO dict for provenance
  sigmond_tui.py      # parse_receiver_channels for sigmond's TUI Activity panel
  core/
    recorder.py            # MeteorScatterRecorder — process-global orchestration
    receiver_manager.py    # ReceiverManager — everything radiod-specific
    stream.py              # ChannelSink — SlotClock anchoring + ring feed
    ring.py                # Ring — process-local deque behind a lock
    slot.py                # SlotWorker — slot harvest, WAV, jt9 fork, reap
    decoder.py             # jt9 resolution, argv, decoded.txt parse, normalize
    cycle_batcher.py       # MeteorScatterCycleBatcher — per-cycle SQLite writes
    ch_tailer.py           # ChTailer — mode log → callhash → spot rows
    hs_uploader_shim.py    # HsPskReporterUploader — the only upload path
    wav.py                 # write_wav() — mono s16le RIFF
tests/                # 16 test modules; fixtures in tests/fixtures/
config/               # meteor-scatter-config.toml.template + help.toml
scripts/              # install.sh, deploy.sh, config-wizard.sh
systemd/              # meteor-scatter@.service template unit
deploy.toml           # sigmond client manifest
```

There is no `core/authority_reader.py` in this build — the §18 timing
authority is read through the suite-shared `hamsci_dsp.timing`
`AuthorityReader` (imported by `core/stream.py` and `core/ch_tailer.py`).

### Delivery mode (`METEOR_SCATTER_DELIVERY_MODE`)

`MeteorScatterRecorder._delivery_mode()` resolves the variable, default
`"direct"`:

| Value | Uploader | `forward_to_pskreporter` | Who posts to PSKReporter |
|---|---|---|---|
| `direct` (code default) | started in-process | `False` | this host, via `HsPskReporterUploader` |
| `deposit` | not started | `True` | the wsprdaemon server's elected forwarder |
| `off` / `none` / `disabled` | not started | `False` | nobody — rows sit in the sink and ride the cycle tar |

A sigmond-managed host lands on **`deposit`**: `deploy.toml`'s
`[contract.instance_env]` seeds `METEOR_SCATTER_DELIVERY_MODE = "deposit"`
into `/etc/meteor-scatter/env/<reporter_id>.env` when sigmond creates the
instance, because on such a host the single hs-uploader daemon owns
egress for every client. Flip to `direct` on a standalone host with no
hs-uploader daemon. The uploader also refuses to start when
`[station].callsign` or `grid_square` is empty (it logs a warning and
returns).

## Per-module responsibilities

### `core/recorder.py` — `MeteorScatterRecorder`

The process-global orchestrator. Holds one `ReceiverManager` per source
(single-radiod deployments pass a one-element list), and owns everything
that is not radiod-specific: the chrony settle gate, the lifetime
keep-alive thread, the cycle batcher, the uploader fan-out, the 60 s
stats thread, the main loop, the systemd watchdog and signal handling.

The **chrony settle gate** (`_wait_for_chrony_settled`) blocks startup
until chrony's `Last offset` has been under `SETTLE_MAX_OFFSET_US`
(default 100 µs) for `SETTLE_REQUIRED_CYCLES` (default 3) consecutive
polls, capped by `SETTLE_TIMEOUT_SEC` (default 60 s). Anchoring a channel
while the host clock is still slewing freezes a non-zero ε₀ into every
subsequent slot timestamp; the gate is what stops that. Missing
`chronyc` is a loud warning, not a failure.

The **watchdog** is deliberately tied to a signal-independent counter.
`_pipeline_progress()` sums each `SlotWorker`'s
`decodes_ok + decodes_fail + slots_empty`, so it advances every T/R
period whenever RTP flows — and at worst every ~60 s via the decode
kill-deadline — *regardless of whether anything decoded*. Meteor pings
are rare; liveness must never depend on spots. A `_ProgressGate` with
`stall_sec=90.0` (under the unit's `WatchdogSec=120`) withholds the
`WATCHDOG=1` ping when that counter freezes, and systemd restarts the
daemon.

### `core/receiver_manager.py` — `ReceiverManager`

One per radiod control plane. Owns the `RadiodControl` connection, the
`ChannelSink` instances, the `MultiStream` instances grouped by
multicast destination, the lifetime-keepalive entries, the per-mode log
file descriptors and `ChTailer`s, and the spool dir at
`<spool_root>/<radiod_id>`.

Channel provisioning is deliberately forgiving. A cold or busy radiod is
slow to confirm a fresh channel, and the old behaviour — ka9q's 5 s hard
verify — let one slow channel abort the whole daemon into a systemd
restart loop. Each channel now gets `METEOR_SCATTER_CHANNEL_VERIFY_TIMEOUT_S`
(default 10 s) × `METEOR_SCATTER_CHANNEL_VERIFY_RETRIES` (default 1
retry, 1.5 s backoff) inside a whole-provisioning budget of
`METEOR_SCATTER_CHANNEL_VERIFY_BUDGET_S` (default 120 s); a channel that
still will not verify is skipped with a warning rather than being fatal.

### `core/stream.py` — `ChannelSink`

One per (mode, frequency). Owns no socket and no thread of its own: it
receives sample batches through the `on_samples` callback a shared
`MultiStream` dispatches after demultiplexing by SSRC.

Its timing model is **anchor once, then defer to RTP**:

1. The first batch anchors a `ka9q.SlotClock` off radiod's GPS-true RTP
   timestamp, mapped to UTC by `hamsci_dsp.timing.acquire_anchor_utc`
   (preferring `ka9q.rtp_to_utc` — radiod's `GPS_TIME` / `RTP_TIMESNAP`
   snapshot — plus hf-timestd's §18 dynamic RTP→UTC offset; falling back
   to the host wall clock when `channel_info` is unavailable).
2. Every later batch is pushed to the ring keyed by its **absolute RTP
   sample offset**, never by a delivered-sample-count projection of UTC.
   The audio handed to `jt9` therefore always lines up with the RTP grid
   point its WAV is labelled with.
3. Re-anchoring happens **only** on a genuine stream restart
   (`on_stream_restored`, fired by `MultiStream` after a real radiod
   outage) — the grid is RTP-driven and drift-immune, so there is no
   per-batch second-guessing of radiod.

Per METROLOGY.md §4.5, the recorder does not diagnose timing health on
its own; that is hf-timestd's job. A badly wrong host clock at anchor
time shows up as decode rate going to zero, not as a client-side
wall-clock complaint.

### `core/ring.py` — `Ring`

A `collections.deque` behind a `threading.Lock`, sized `RING_SECONDS =
60.0` of audio. Process-local by design — there are no cross-process
consumers, so no SysV IPC and no shared memory.

### `core/slot.py` — `SlotWorker`

One daemon thread per channel, polling the ring every 500 ms. Slot
boundaries come from `ka9q.SlotClock` (`clock.advance(latest_rtp)`); the
worker extracts each completed slot's exact sample window from the ring
by absolute RTP offset, waits `SETTLE_SEC = 1.5`, writes the WAV, and
forks the decoder.

`jt9` is unlike `decode_ft8` in a way that shapes this module: it writes
its decodes to a **`decoded.txt`** file in its `-a` data directory and
prints only a `<DecodeFinished> <n> …` sentinel to stdout. So the worker
keeps a stable per-channel workdir, pre-touches the `plotspec` /
`decdata` sentinels jt9 opens, runs with `cwd=<workdir>`, and after each
run reads the *lines appended to `decoded.txt` since the previous run* —
the same line-count-diff pattern wspr-recorder uses for
`fst4_decodes.dat`. A child still alive after `DECODE_TIMEOUT_SEC = 60.0`
is killed, so a hung decode cannot leak its stdio FDs and spool WAV
forever. `VALID_DECODER_KINDS = ("jt9",)` — there is exactly one decoder
backend.

### `core/decoder.py` — jt9 resolution, argv and parsing

`resolve_jt9_binary()` prefers an explicit `paths.decoder_jt9` /
`paths.decoder` override that exists, else `shutil.which("jt9")`. **No
decoder binaries are bundled in this repo** — jt9 3.0.2 is built from
pinned WSJT-X source on the host by sigmond's `_build_wsjtx_decoders`
and installed to `/usr/local/bin/jt9` (GPLv3: build from retained source
on-host rather than redistribute a modified binary).

`build_jt9_cmd()` produces:

```
jt9 -Y --msk144 -p <tr_period_sec> -f 1500 -a <workdir> <wav>
```

`-Y` is the callhash hinge: it makes jt9 emit an unresolved compound
callsign as the numeric 22-bit `<NNNNNNN>` form instead of the opaque
`<...>`, which is what lets the shared table substitute the plaintext
back later. Without it the spot would simply lose its call.

`parse_decoded_txt_line()` is deliberately tolerant (HHMM vs HHMMSS time
column; sync char present as its own token or absent) and returns
`{snr, dt, audio_freq_hz, sync, message}` — never the time token, because
the authoritative UTC comes from the slot anchor, not from jt9's
WAV-filename-derived time column. `normalize_log_line()` renders

```
YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>
```

where `abs_freq_hz` = channel dial + jt9's audio offset (the true RF
frequency) and `&` is MSK144's sync indicator (FT8 uses `~`, FT4 `+`,
JT65 `#`, JT9 `@`). That one character is how `ch_tailer` and
`smd watch meteor` route the line.

### `core/ch_tailer.py` — `ChTailer`

One daemon thread per (radiod, mode) log file. Each polling pass:

1. Feeds the whole new chunk to `CallHashTable.observe()` first, so any
   `<call>` announcements land in the cumulative table before per-line
   parsing.
2. Reads the §18 timing authority once per chunk (all decodes in a chunk
   share the slot's authority state), degrading to
   `standalone_timing_authority()` when hf-timestd is absent or stale.
3. Parses each line via `parse_jt9_msk144_line` → `callhash.parse_message`,
   producing `time`, `mode`, `decoder_kind="jt9"`, `snr_db`, `dt`,
   `frequency` (+ `frequency_mhz`), hash-resolved `message`, `tx_call`,
   `rx_call`, `grid`, `report`. `score` and `spectral_width_hz` are
   `None` — jt9 reports a calibrated dB SNR, not ft8_lib's internal score.
4. Stamps `timing_authority`, `host_call`, `host_grid`, `radiod_id`,
   `instance` (legacy, = radiod_id), `reporter_id`, `rx_source`
   (`radiod:<status>`), `frequency_bucket_hz` (100 Hz bucket, PSKReporter's
   own dedup tolerance), `processing_version` and `forward_to_pskreporter`.
5. Hands the rows to the cycle batcher when present, else writes them
   directly (the legacy single-tailer path kept for tests).

The callhash table is persisted every `CALLHASH_SAVE_INTERVAL_SEC`
(5 min) and on shutdown, so resolution accumulates across restarts — and
across modes: a call learned on FT8 or WSPR resolves an MSK144 hash and
vice-versa.

### `core/cycle_batcher.py` — `MeteorScatterCycleBatcher`

Sits between the tailers and SQLite. Accepts rows from any thread and
flushes once per `(cycle, source)` on its own writer thread, which is
what keeps a single thread-bound `sqlite3.Connection` no matter how many
tailers feed it. It also emits the per-cycle log line `smd watch meteor`
renders. `MSK144_CYCLE_SEC = 15.0` is the **bucketing grid** — a spot's
time is floored to a 15 s boundary to compute `cycle_start_iso` — and is
independent of the configured `tr_period_sec`. The flush deadline
(`METEOR_SCATTER_CYCLE_DEADLINE_SEC`, default 10 s) is the wall-clock
window after the first spot lands in a batch.

### `core/hs_uploader_shim.py` — `HsPskReporterUploader`

The sole upload path, and only under `DELIVERY_MODE=direct`. It drives
the hs-uploader `Pipeline` + `Uploader` with the `PskReporterTcp`
transport, which owns the TCP socket end to end — there is no
`pskreporter` subprocess and no third-party PSKReporter library on the
runtime path.

Source selection: `SqliteSource` over sigmond's sink, with `extra_where`
filters scoping the queue to this daemon's `radiod_id` so multi-instance
is safe by construction; when no sink is writable it falls back to
`FileTreeSource` over the per-slot `.spots.txt` files, deleted on ack.
`PUMP_INTERVAL_SEC = 30.0` *is* the upload cadence. Watermark and retry
state live in `/var/lib/hs-uploader/watermarks.db`, so a restart resumes
rather than re-deriving from "now".

Cross-rx dedup is **opt-in** (`METEOR_SCATTER_DIRECT_DEDUP=1`, default
off): its window-function CTE trips `sqlite3.OperationalError: disk I/O
error` every 30 s on hosts where wspr-recorder is also writing
`sink.db`, which stalls the direct pipeline. Single-source hosts have
nothing to dedup anyway.

### `core/wav.py` — `write_wav()`

A minimal RIFF writer: mono, 16-bit signed PCM, little-endian, matching
what `pcmrecord` produces. (Its module docstring still says "for
decode_ft8 input" — a leftover of the skeleton; the format is what jt9
consumes too.)

### `cli.py`

`inventory` / `validate` / `version` / `daemon` / `status` /
`config {init,edit,show,apply}` / `env {show,apply}`. A
stdout-cleanliness guard routes all logging to stderr for the "quiet"
surfaces (`inventory`, `validate`, `version`, and `config`/`env`
`show`/`apply`) because the whiptail wizard and sigmond parse their
stdout. Log level resolves `--log-level` → `METEOR_SCATTER_LOG_LEVEL` →
`CLIENT_LOG_LEVEL` → `INFO`, and is re-read on `SIGHUP` without
restarting RTP streams.

## Key design decisions

- **One decode mode.** `MODES = ("msk144",)`. The per-mode loops
  inherited from the FT4/FT8 skeleton iterate cleanly over a
  one-element tuple; that is the only reason the plural survives.
- **Templated systemd unit.** `meteor-scatter@<instance>.service`, one
  instance per radiod, started and stopped independently.
- **radiod is identified by its mDNS status name**, never an IP —
  `[[radiod]].status` per RADIOD-IDENTIFICATION.md §3.1. The legacy
  `id` / `radiod_status` fields were removed at the Phase 6 cutover;
  `resolve_radiod_block` now tells the operator to run
  `sudo smd radiod migrate --yes`.
- **ka9q-python owns the multicast destination.** `ensure_channel()` is
  never called with `destination=`; the resolved address is read back
  from `ChannelInfo` for the inventory payload (contract §7).
- **Subprocess only for decoding.** The uploader is in-process.
- **WAV deleted after decode** (`paths.keep_wav = false` default).
- **Fail fast on an unconfigured radiod.** The placeholder status
  `<configure-via-config-init>` can never resolve, so the daemon exits
  `EX_CONFIG` (78) and the unit's `RestartPreventExitStatus=78` stops it
  cleanly instead of crash-looping a config that can never succeed.
- **PSWS station/instrument IDs are optional** — meteor-scatter does not
  require them.

## How a ping becomes a spot

1. A meteor trail ionizes; a distant WSJT-X station's MSK144 burst
   reaches the antenna for a few tens of milliseconds.
2. radiod's `usb` channel (12 kHz, `s16be`, ~300–2950 Hz passband)
   carries it in the RTP multicast stream. **Do not narrow that filter** —
   MSK144 occupies ~2.4–2.5 kHz around the 1500 Hz audio centre and a
   tighter filter clips the sidebands and kills decodes.
3. `MultiStream` demultiplexes by SSRC; `ChannelSink.on_samples` pushes
   the batch into the ring at its absolute RTP offset.
4. `SlotWorker` sees the T/R slot complete, extracts exactly that
   window, writes `<spool>/<radiod_id>/msk144/YYMMDD_HHMMSS.wav`.
5. `jt9 -Y --msk144 -p <tr> -f 1500 -a <wd> <wav>` runs; MSK144's 72 ms
   LDPC-FEC'd frames mean a single ping can carry a whole decode, and
   jt9 finds *every* ping in the slot.
6. The delta appended to `decoded.txt` is normalized and appended to
   `/var/log/meteor-scatter/<radiod_id>-msk144.log`.
7. `ChTailer` parses that line, resolves any `<NNNNNNN>` hash from the
   shared table, stamps provenance, and hands the row to the batcher.
8. The batcher flushes the cycle into `psk.spots` with `mode="msk144"`,
   `schema_version=2`.
9. Either the in-process uploader POSTs it to pskreporter.info (mode
   `msk144` → `MSK144`), or — in `deposit` mode — the row carries
   `forward_to_pskreporter=True` and the wsprdaemon server's forwarder
   owns that hop.

## Testing

16 test modules under `tests/`, fixtures in `tests/fixtures/`:
`test_decoder.py` (argv, decoded.txt parsing, normalization),
`test_slot.py` (cadence, WAV, decoder fork/reap), `test_ch_tailer.py`
(line parse, callhash substitution, row shape), `test_cycle_batcher.py`,
`test_stream_anchoring.py` (the anchor-once/RTP-defer invariant),
`test_watchdog_gate.py`, `test_settle_env.py`, `test_receiver_manager.py`,
`test_lifetime.py`, `test_contract.py`, `test_config.py`,
`test_config_show_apply.py`, `test_configurator.py`,
`test_status_listener_wiring.py`, `test_ring.py`, `test_wav.py`.

```bash
uv sync --extra dev && uv run pytest tests/ -v     # canonical
PYTHONPATH=src python3 -m pytest tests/ -v         # no venv needed
```
