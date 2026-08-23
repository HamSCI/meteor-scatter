# Configuration reference

> **Audience:** operator/contributor
> **Status:** current
> **Verified against:** meteor-scatter bac2116 on 2026-08-23 — code
> **Canonical for:** every meteor-scatter TOML key and environment variable

Config file: `/etc/meteor-scatter/<instance>.toml` (preferred) or the
legacy shared `/etc/meteor-scatter/meteor-scatter-config.toml`; override
with `--config` or `METEOR_SCATTER_CONFIG`. TOML format.

A starter template lives at
[config/meteor-scatter-config.toml.template](../config/meteor-scatter-config.toml.template).
Don't hand-copy it — `meteor-scatter config init` (or
`smd config init meteor-scatter`) renders it with the station identity
already filled in.

Every default below is read from `src/meteor_scatter/config.py`
(`DEFAULTS`, `MSK144_CADENCE_SEC`, `DEFAULT_SAMPLE_RATE`,
`DEFAULT_PRESET`, `DEFAULT_ENCODING`) or from the `.get(key, default)`
call site named in the row.

## `[station]`

| Key | Default | Meaning |
|---|---|---|
| `callsign` | *(none — warn)* | Your callsign; the PSKReporter reporter identity. `validate` warns when empty, and the uploader refuses to start without it. |
| `grid_square` | *(none — warn)* | Maidenhead locator, 6–10 characters. Same warn/refuse behaviour as `callsign`. |
| `antenna` | `""` | Free-form antenna text passed to PSKReporter alongside each spot (`recorder._start_uploaders`). Not in the template; add it if you want it published. |
| `psws_station_id` | *(unset)* | Optional PSWS station ID. meteor-scatter does not need it. |
| `psws_instrument_id` | *(unset)* | Optional PSWS instrument ID. |

## `[paths]`

| Key | Default | Meaning |
|---|---|---|
| `spool_dir` | `/var/lib/meteor-scatter` | WAV spool root. Slots land in `<spool_dir>/<radiod_id>/msk144/`. |
| `log_dir` | `/var/log/meteor-scatter` | Spot-log dir. One append-only file per radiod: `<radiod_id>-msk144.log`. |
| `keep_wav` | `false` | Keep slot WAVs after decode. Debug only — at 12 kHz mono s16 a kept slot is ~24 KB/s per channel. |
| `decoder_kind` | `"jt9"` | Which decoder backend `SlotWorker` uses. `VALID_DECODER_KINDS = ("jt9",)` — jt9 is the only accepted value. |
| `decoder_jt9` | `""` | Explicit path to a jt9 binary. Empty (the default) means "resolve `jt9` from PATH", where sigmond's from-source WSJT-X 3.0.2 build installs it as `/usr/local/bin/jt9`. |
| `decoder` | `""` | Legacy alias for `decoder_jt9`, consulted only when `decoder_jt9` is absent. Prefer `decoder_jt9`. |
| `pskreporter_tcp` | `true` | Historical knob. The `PskReporterTcp` transport is TCP-only, so setting it `false` logs "ignored" and changes nothing. |

An explicit decoder override that resolves to neither an existing file
nor a `which`-able name is a `validate` **warning** (not a failure) — the
recorder still records slots, it just cannot decode them.

## `[processing]`

Not present in the template; defaulted by `config.load_config()`.

| Key | Default | Meaning |
|---|---|---|
| `radiod_lifetime_frames` | `6000` | radiod `LIFETIME` tag, in radiod main-loop frames (~50 Hz at the default 20 ms blocktime, so 6000 ≈ 2 min). Channels self-destruct after this many frames without a keep-alive; the recorder refreshes every `frames / 4` seconds while running, so a crashed daemon leaves no residual channels on radiod. `0` = infinite (no `LIFETIME` tag, no keep-alive). Must be a non-negative int, else `load_config` raises. |

## `[timing]` (optional)

| Key | Default | Meaning |
|---|---|---|
| `chain_delay_ns` | `0` | Contract §8 standalone fallback: nanoseconds of SDR + radiod + multicast latency. Almost always overridden by `RADIOD_<ID>_CHAIN_DELAY_NS` from `/etc/sigmond/coordination.env`. Surfaced in inventory as `chain_delay_ns_applied`; MSK144 spot times are T/R-slot-quantized, so this is a contract hook, not a correction meteor-scatter applies. |

## `[instance]` (optional, per-instance configs)

| Key | Default | Meaning |
|---|---|---|
| `reporter_id` | *(none)* | The per-instance reporter identity written onto every spot row (contract §19). Resolution order: `[instance].reporter_id` → `STATION_REPORTER_ID` from coordination.env → the radiod hostname, with a loud warning, because that fallback misattributes uploads all the way to PSKReporter. |

## `[[radiod]]` (one or more)

| Key | Default | Meaning |
|---|---|---|
| `status` | **required** | The radiod control/status **mDNS multicast name**, e.g. `sigma-rx888mk2-status.local` — never an IP (RADIOD-IDENTIFICATION.md §3.1). The template ships the sentinel `<configure-via-config-init>`, which `validate` fails on and the daemon refuses to start against (exit 78). |

The legacy `id` and `radiod_status` fields were removed at the Phase 6
cutover. A config that still has them fails with a pointer to
`sudo smd radiod migrate --yes`.

### `[radiod.msk144]`

The only mode block — `config.MODES = ("msk144",)`.

| Key | Default | Meaning |
|---|---|---|
| `freqs_hz` | `[]` (warn) | Dial frequencies to monitor. Template ships 10 m (`28_145_000`) and 6 m (`50_260_000`), the conventional WSJT-X MSK144 monitoring channels. Empty → `validate` warns and the instance provisions no channels. |
| `sample_rate` | `12000` | WSJT-X protocol requirement. |
| `preset` | `"usb"` | radiod preset. Its filter (low +50, high +3k → ~2950 Hz passband) is the full SSB audio width a real WSJT-X station receives on. **Do not narrow it:** MSK144 is ~2.4–2.5 kHz wide (2000 baud MSK) centred on 1500 Hz, and a tighter filter clips the sidebands and kills decodes. |
| `encoding` | `"s16be"` | Wire encoding requested from radiod. |
| `tr_period_sec` | `30` (`MSK144_CADENCE_SEC = 30.0`) | The MSK144 T/R sequence length in seconds. This is simultaneously the recorded slot length, the decode cadence, and `jt9 -p`. It **must match the T/R period the stations you monitor use** — a mismatch still decodes (MSK144 frames are self-contained) but the same TX shows up in adjacent slots with a wandering `dt`. Stock WSJT-X defaults MSK144 to 30 s; 5/10/15 s are also offered, and 15 s is the 2 m VHF meteor-ping convention. |

Note that the sink's cycle *bucketing* grid is a fixed
`MSK144_CYCLE_SEC = 15.0` in `core/cycle_batcher.py`, independent of
`tr_period_sec` — a spot's time is floored to a 15 s boundary to compute
`cycle_start_iso`.

## Resolution rules

### Which config file is loaded

`config.resolve_config_path()`, most to least specific:

1. `--config <path>` — always wins.
2. `$METEOR_SCATTER_CONFIG`.
3. `/etc/meteor-scatter/<instance>.toml` when `--instance` was passed
   and the file exists (the per-instance v0.8 world).
4. `/etc/meteor-scatter/meteor-scatter-config.toml` — legacy shared, with
   a one-line `DeprecationWarning` when an instance *was* given but its
   per-instance file does not exist (the host has not been through
   `sudo smd instance migrate`).
5. The same legacy path silently when no instance was given.

`inventory --json` and `validate --json` both report the absolute path
they loaded (`config_path`), so there is never a guess about which file
is live.

### Which `[[radiod]]` block is used

`config.resolve_radiod_block()` matches `--radiod-id` against each
block's `status` field. With no `--radiod-id`, the config must contain
exactly one `[[radiod]]` block or the call raises. The systemd unit
passes `--radiod-id %i` alongside `--instance %i`.

### `chain_delay_ns` precedence

`RADIOD_<ID>_CHAIN_DELAY_NS` in the environment (sigmond writes it into
`/etc/sigmond/coordination.env`, uppercased with `-` and `.` → `_`) wins
over `[timing].chain_delay_ns`.

## Environment variables honored

Read from `/etc/sigmond/coordination.env` and
`/etc/meteor-scatter/env/<instance>.env` by the unit's `EnvironmentFile=`
lines (the `%i` form is the working one — `%I` unescapes dashes into
slashes).

| Variable | Default | Effect |
|---|---|---|
| `METEOR_SCATTER_CONFIG` | — | Config path override (below `--config`). |
| `METEOR_SCATTER_DELIVERY_MODE` | `direct` | `direct` runs the in-process PSKReporter uploader; `deposit` leaves rows in the sink flagged `forward_to_pskreporter=True` for the wsprdaemon server's forwarder; `off`/`none`/`disabled` publish nothing. sigmond seeds `deposit` from `deploy.toml [contract.instance_env]`. |
| `METEOR_SCATTER_DIRECT_DEDUP` | `0` | Opt in to the cross-rx dedup CTE in the direct pipeline. Off by default because the CTE trips `disk I/O error` when `sink.db` is shared with wspr-recorder. |
| `METEOR_SCATTER_LOG_LEVEL` | — | Log level; beaten only by `--log-level`. Re-read on `SIGHUP`. |
| `CLIENT_LOG_LEVEL` | — | Suite-wide fallback for the above (contract §11). |
| `METEOR_SCATTER_CYCLE_DEADLINE_SEC` | `10.0` | Wall-clock window after the first spot lands in a cycle batch before it flushes. |
| `METEOR_SCATTER_CHANNEL_VERIFY_TIMEOUT_S` | `10` | Per-channel radiod verify budget. |
| `METEOR_SCATTER_CHANNEL_VERIFY_RETRIES` | `1` | Retries per channel (1.5 s backoff). |
| `METEOR_SCATTER_CHANNEL_VERIFY_BUDGET_S` | `120` | Whole-provisioning deadline; channels still unverified after it are skipped with a warning. |
| `METEOR_SCATTER_SETTLE_MAX_OFFSET_US` | `100` | chrony settle gate: ceiling on \|Last offset\|. |
| `METEOR_SCATTER_SETTLE_REQUIRED_CYCLES` | `3` | Consecutive settled polls required. |
| `METEOR_SCATTER_SETTLE_POLL_SEC` | `5.0` | Settle-gate poll interval. |
| `METEOR_SCATTER_SETTLE_TIMEOUT_SEC` | `60.0` | Cap on the settle wait; timing out logs loudly and proceeds degraded. |
| `SIGMOND_SQLITE_PATH` | `/var/lib/sigmond/sink.db` | Sink location. Its presence is also what selects `SqliteSource` over the `.spots.txt` file fallback. |
| `STATION_REPORTER_ID` | — | Site-wide reporter id; the fallback below `[instance].reporter_id`. |
| `STATION_CALL` / `STATION_GRID` | — | Wizard pre-fills only (contract §14.3). |
| `SIGMOND_INSTANCE` / `SIGMOND_RADIOD_STATUS` / `SIGMOND_RADIOD_COUNT` / `SIGMOND_RADIOD_INDEX` | — | §14.3 env bag consumed by `config init --non-interactive`. |
| `RADIOD_<ID>_CHAIN_DELAY_NS` | — | Per-radiod chain delay (contract §8). |
| `METEOR_SCATTER_HELP_TOML` / `METEOR_SCATTER_CLI` | — | Wizard plumbing: override the help sidecar and the CLI binary the wizard shells out to. |

⚠ **`meteor-scatter env apply` manages no keys in this build.**
`configurator._ENV_WRITABLE_KEYS` is the empty set, so every payload key
is rejected with "unknown / unmanaged keys". `env show` works. Set
`METEOR_SCATTER_DELIVERY_MODE` and friends by editing
`/etc/meteor-scatter/env/<instance>.env` directly — which is also what
sigmond does when it seeds the instance.

## Validating your config

```bash
meteor-scatter validate --json | jq
```

`ok` is false when any issue has `severity: "fail"`.

**Fails:** no `[[radiod]]` blocks; a block with no `status`; a `status`
still set to the `<configure-via-config-init>` placeholder; an SSRC
collision — two entries sharing `(freq, preset, sample_rate, encoding)`,
which ka9q-python would silently collapse to one channel.

**Warns:** empty `station.callsign`; empty `station.grid_square`; no
MSK144 frequencies configured; an unresolvable decoder override.
