# Configuration reference

Config file: `/etc/meteor-scatter/meteor-scatter-config.toml` (override
with `--config` or `METEOR_SCATTER_CONFIG`). TOML format.

A starter template lives at
[config/meteor-scatter-config.toml.template](../config/meteor-scatter-config.toml.template).

## `[station]`

Operator identity. Used by `pskreporter-sender`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `callsign` | string | — | Required for spot uploads. `validate` warns if empty. |
| `grid_square` | string | — | Maidenhead, ≥ 6 chars recommended. `validate` warns if empty. |
| `psws_station_id` | string | unset | Optional. Only used by operators who also run a [PSWS](https://hamsci.org/psws) station. Ignored otherwise. |
| `psws_instrument_id` | string | unset | Same. |

## `[paths]`

Filesystem and external binary locations. Defaults are usually fine.

| Key | Type | Default | Notes |
|---|---|---|---|
| `spool_dir` | path | `/var/lib/meteor-scatter` | WAV files go under `<spool_dir>/<radiod_id>/{ft8,ft4}/`. |
| `log_dir` | path | `/var/log/meteor-scatter` | Process and spot logs. |
| `decoder` | path | `/usr/local/bin/decode_ft8` | `validate` warns if missing or not executable. |
| `pskreporter` | path | `/usr/local/bin/pskreporter-sender` | `validate` warns if missing. May point at a venv-installed copy, e.g. `/opt/meteor-scatter/venv/bin/pskreporter-sender`. |
| `keep_wav` | bool | `false` | If `true`, decoded WAVs stay in spool. Useful for debugging; expensive on disk (~10 MB/min/channel). |
| `pskreporter_tcp` | bool | `false` | If `true`, `pskreporter-sender` is invoked with `--tcp`. Default UDP is more battle-tested. |

## `[timing]` (optional)

Standalone fallback for chain-delay correction. Sigmond-managed
deployments override this via `RADIOD_<ID>_CHAIN_DELAY_NS` in
`/etc/sigmond/coordination.env`; standalone deployments may set it
here.

| Key | Type | Default | Notes |
|---|---|---|---|
| `chain_delay_ns` | int | `0` | Nanosecond correction. See [SIGMOND-CONTRACT.md §8](SIGMOND-CONTRACT.md). |

## `[[radiod]]` (one or more)

Each block defines one radiod the daemon will bind to. The block's
`id` becomes the systemd instance name (`meteor-scatter@<id>.service`).

| Key | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | Required. Used in unit name, spool path, log filenames. Convention: `<host>-<frontend>` (e.g. `bee1-rx888`). |
| `radiod_status` | string | — | Required. mDNS hostname of radiod's status multicast (e.g. `bee1-status.local`). **Never an IP.** Overridden by `RADIOD_<ID>_STATUS` env var if set. |

### `[radiod.ft8]` and `[radiod.ft4]`

Channel definitions per mode. Both blocks are optional, but at least
one should have frequencies or the instance has nothing to do.

| Key | Type | Default | Notes |
|---|---|---|---|
| `sample_rate` | int | `12000` | Hz. `decode_ft8` requires 12000. |
| `preset` | string | `"usb"` | radiod preset name. |
| `encoding` | string | `"s16be"` | RTP payload encoding. |
| `freqs_hz` | int[] | `[]` | List of dial frequencies in Hz. Each becomes one channel (one RTP stream, one ring buffer, one slot worker). |

## Resolution rules

### Which `[[radiod]]` block is used

The `daemon` subcommand requires `--radiod-id <id>` (provided by the
systemd unit as `%i`). It selects the matching `[[radiod]]` by `.id`.

For `inventory` and `validate` (which describe all instances), every
`[[radiod]]` block is processed.

### `radiod_status` precedence

1. `RADIOD_<ID_UPPERCASE_UNDERSCORED>_STATUS` env var
   (e.g. `RADIOD_BEE1_RX888_STATUS=bee1-status.local`)
2. `radiod_status` field in the `[[radiod]]` block

If neither is set, `validate` fails.

### `chain_delay_ns` precedence

1. `RADIOD_<ID>_CHAIN_DELAY_NS` env var (sigmond-supplied via
   `coordination.env`)
2. `[timing].chain_delay_ns` in config
3. `0`

## Environment variables honored

| Var | Purpose |
|---|---|
| `METEOR_SCATTER_CONFIG` | Default config path. |
| `METEOR_SCATTER_LOG_LEVEL` | Log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`). |
| `CLIENT_LOG_LEVEL` | Sigmond-supplied log level. Used if `METEOR_SCATTER_LOG_LEVEL` is unset. See [SIGMOND-CONTRACT.md §11](SIGMOND-CONTRACT.md). |
| `RADIOD_<ID>_STATUS` | mDNS hostname for that radiod (overrides config). |
| `RADIOD_<ID>_CHAIN_DELAY_NS` | Chain-delay correction for that radiod. |

CLI `--log-level` overrides env. SIGHUP re-reads `*_LOG_LEVEL` and
applies it live.

## Validating your config

```bash
meteor-scatter validate --json --config /etc/meteor-scatter/meteor-scatter-config.toml
```

Output is JSON with `ok: true|false` and an `issues` array. Exit code
0 if no `severity: fail` issues. See
[OPERATIONS.md](OPERATIONS.md#validating-and-inventorying) for the
schema.
