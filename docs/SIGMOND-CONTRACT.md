# Sigmond client contract conformance

> **Audience:** contributor
> **Status:** current
> **Verified against:** meteor-scatter bac2116 on 2026-08-23 — code
> **Canonical for:** how meteor-scatter maps onto the HamSCI client contract

meteor-scatter implements the [HamSCI client contract][contract] (v0.8),
maintained in the sigmond repository at [`docs/CLIENT-CONTRACT.md`][contract].
`src/meteor_scatter/contract.py` declares `CONTRACT_VERSION = "0.8"` and
`deploy.toml` declares `contract_version = "0.8"`; the catalog entry
(`sigmond/etc/catalog.toml [client.meteor-scatter]`) matches.

This is a section-by-section map of what meteor-scatter actually does.
The contract is the norm; where the two disagree the contract wins and
this page is the bug. Sections marked **not implemented** are honest
gaps, not omissions from this page.

**Scope note.** meteor-scatter was scaffolded from psk-recorder and
inherits its contract surface almost verbatim; the differences are the
single mode (`msk144`), the decoder (`jt9`, not `decode_ft8`), and the
shared-not-separate sink namespace.

## What the contract is for

Sigmond installs, configures, starts, monitors and coordinates a fleet
of independent clients without knowing anything client-specific. The
contract is the interface that makes that possible: a client that
implements it can be dropped into `/opt/git/sigmond/<name>` and
discovered from its own `deploy.toml`, with no sigmond-side edits.

## §1 — Native config

✅ Implemented. `/etc/meteor-scatter/<instance>.toml` (preferred) or the
legacy shared `/etc/meteor-scatter/meteor-scatter-config.toml`, plain
TOML, loaded by `config.load_config()` with defaults merged in. Sigmond
never edits it — it invokes meteor-scatter's own `config init|edit`
(§14). See [CONFIG.md](CONFIG.md).

## §2 — Binding to radiod by status name

✅ Implemented, at the v0.8 cutover point. The `[[radiod]]` block's
canonical identifier is its **mDNS control/status multicast name**
(`status = "sigma-rx888mk2-status.local"`), per
RADIOD-IDENTIFICATION.md §3.1 — never an IP.

`config.resolve_radiod_block()` matches `--radiod-id` against `status`
and has **removed** acceptance of the legacy `id` field; a config that
still carries `id` / `radiod_status` fails with an explicit pointer to
`sudo smd radiod migrate --yes`. `config.resolve_radiod_status()` and
`derive_source_key()` (→ `radiod:<status>`, matching
`sigmond.sources.SourceKey` and wspr-recorder's `SourceConfig.key`)
build everything else from that one field.

The unconfigured sentinel `<configure-via-config-init>` mirrors
`sigmond.harmonize._RADIOD_STATUS_PLACEHOLDER`: `validate` fails on it
and the daemon exits `EX_CONFIG` (78) rather than crash-looping.

## §3 — Self-describe CLI

✅ `inventory --json`, `validate --json`, `version --json`, all pure
stdout JSON with logging forced to stderr by the CLI's
stdout-cleanliness guard.

`contract.build_inventory()` emits:

| Top-level key | Value |
|---|---|
| `client` | `"meteor-scatter"` |
| `version` | `importlib.metadata.version("meteor-scatter")`, falling back to `"0.4.0"` |
| `contract_version` | `"0.8"` |
| `config_path` | absolute path actually loaded (§12.3) |
| `git` | `version.GIT_INFO` when present |
| `log_paths` | `{<radiod_id>: {"spots": {"msk144": "<log_dir>/<radiod_id>-msk144.log"}}}` (§10) |
| `log_level` | effective root level name (§11) |
| `instances` | one entry per `[[radiod]]` block |
| `deps` | git + pypi declarations |
| `issues` | the same list `validate` returns |

Per instance:

| Field | Value |
|---|---|
| `instance` / `radiod_id` / `radiod_status_dns` | all three are the mDNS status name — the only functional identifier (§2, RADIOD-IDENTIFICATION.md §3.2) |
| `host` | `"localhost"` |
| `data_destination` | `null` — see §7 |
| `ka9q_channels` / `frequencies_hz` | count and sorted list of `[radiod.msk144].freqs_hz` |
| `modes` | `["msk144"]` when frequencies are configured, else `[]` |
| `data_sinks` | two `kind="file"` entries — the spool and the log dir (§17) |
| `uses_timing_calibration` / `provides_timing_calibration` | `true` / `false` |
| `chain_delay_ns_applied` | `RADIOD_<ID>_CHAIN_DELAY_NS` from env, else `null` (§8) |
| `timing_authority_applied` | the block the running daemon left at `<spool>/<radiod_id>/timing-authority.json`; `null` when stale, absent, or the daemon anchors without an authority (§18.5) |

⚠ Two shapes to know about: the payload carries **no `templated_units`
key** (sigmond reads the unit list from `deploy.toml [systemd].units`
instead), and `deps.git` still names `ka9q-radio` with the note "jt9
--msk144 decoder (bundled in-repo)", which is doubly wrong — jt9 comes
from WSJT-X, and nothing is bundled (see §"Known drift" below).

An unreadable or invalid config does **not** crash `inventory`: the CLI
catches it and emits a well-formed payload whose `issues` carries a
`severity: "fail"` explanation, because sigmond probing as the operator
against a mode-0640 service-user-owned config is a normal condition.

## §4 — Systemd units

✅ One templated unit, `systemd/meteor-scatter@.service`, installed to
`/etc/systemd/system/` and declared in `deploy.toml [systemd].units`.
`Type=notify`, `WatchdogSec=120`, `NotifyAccess=main`, `Restart=always`,
`RestartPreventExitStatus=78`, `TimeoutStartSec=180`, hardened with
`ProtectSystem=strict` + explicit `ReadWritePaths` (including
`/var/lib/sigmond`, without which the sink writer silently no-ops) and
`ReadOnlyPaths=/etc/meteor-scatter`, and bounded by `MemoryMax=1G` /
`MemorySwapMax=0` because each concurrent jt9 child mmaps ~60 MB.

`EnvironmentFile=` reads `/etc/sigmond/coordination.env` then
`/etc/meteor-scatter/env/%i.env` (and `%I.env` for hosts that worked
around the old bug — `%I` unescapes dashes into slashes, so `%i` is the
working key).

## §5 — Deploy manifest

✅ `deploy.toml` declares `[package]` (name, version 0.4.0,
contract_version 0.8, license), `[contract.config]` init/edit commands,
`[contract.instance_env]` greenfield env defaults, `[build]` steps and
`produces`, `[[install.steps]]` (link the CLI, link the unit, render the
config `if_absent`, mkdir spool + log owned `meteorscat:meteorscat`),
`[systemd].units`, `[[deps.git]]` / `[[deps.pypi]]` / `[[deps.apt]]`,
sigmond UI hooks under `[client_features]`, and one
`[[hs_uploader.pipeline]]`.

The `[client_features]` hooks are what register meteor-scatter with
sigmond's UI without a sigmond-side edit:

| Hook | Value | Effect |
|---|---|---|
| `watch.verb` | `meteor` | `smd watch meteor` — per-cycle decode activity |
| `verifier.verb` / `.kind` | `psk` / `spot_queue` | `smd admin verifier report --target psk` covers MSK144, since the rows share `psk.spots` |
| `receiver_channels` | `sigmond_tui.parse_receiver_channels` | the TUI Activity panel's channel list |

The `[[hs_uploader.pipeline]]` block deliberately declares the **same**
`psk-pskreporter` pipeline psk-recorder declares (`mode IN
(ft8, ft4, msk144)`, `forward_to_pskreporter = 0`). `smd admin uploader
manifest` dedups identical pipelines by name, so the two collapse to one
where both clients are enabled, while a meteor-only host still gets
egress.

## §6 — Talking to radiod

✅ Exclusively through `ka9q-python` (`RadiodControl.ensure_channel`,
`MultiStream`, `SlotClock`, `StatusListener`). meteor-scatter never
speaks radiod's control protocol directly and never runs radiod itself.
`deploy.toml` and the catalog both declare `requires = ["ka9q-python",
"ka9q-radio"]` — which is also why restarting it through `smd` bounces
the radio (see [OPERATIONS.md](OPERATIONS.md)).

## §7 — Deterministic data multicast destination

✅ `ensure_channel()` is never called with `destination=`; ka9q-python
allocates it and meteor-scatter reads the resolved address back from
`ChannelInfo`.

⚠ The inventory's per-instance `data_destination` is hardcoded `null` in
`contract.build_inventory` — the builder works from the config file, not
from a live channel, so it has no `ChannelInfo` to read. The contract
permits `null` where the client has not resolved a destination; a client
that has channels up should report the resolved address, so this is a
partial. The *runtime* behaviour (never specifying a destination) is
fully conformant.

## §8 — Radiod-scoped facts: chain delay

✅ Read as a hook. `contract.build_inventory` derives the env key
`RADIOD_<ID>_CHAIN_DELAY_NS` (status name uppercased, `-` and `.` → `_`)
and surfaces the value as `chain_delay_ns_applied`, `null` when unset.
`[timing].chain_delay_ns` is the standalone fallback.

Not *applied* to sample→UTC conversion, deliberately: MSK144 spot times
are quantized to the T/R slot boundary, which is orders of magnitude
coarser than any chain delay.

## §9 — Reference implementations

meteor-scatter is not one. psk-recorder is the greenfield v0.3
reference; meteor-scatter is a *descendant* of that reference, and this
repo's documentation drift (below) is the cautionary tale attached to
that lineage.

## §10 — Logging discipline and discovery

✅ The process log goes to the journal (`StandardOutput=journal`,
`SyslogIdentifier=meteor-scatter@%I`) and `log_paths` lists only the
file-based spot log, keyed by radiod id → `{"spots": {"msk144": …}}`, so
`smd log --files` finds it. `smd log meteor-scatter` covers the journal.

## §11 — Runtime log level

✅ `--log-level` → `METEOR_SCATTER_LOG_LEVEL` → `CLIENT_LOG_LEVEL` →
`INFO` (`cli._resolve_log_level`), re-resolved on `SIGHUP`
(`cli._install_sighup_handler`) without restarting RTP streams, and
reported as `log_level` in inventory.

## §12 — Validate hardening (v0.4)

| Item | Status |
|---|---|
| **12.1 Entry-point reachability (MUST)** | ⚠ **not checked by `validate`.** The property itself holds — `cli.py` carries the `if __name__ == "__main__": main()` guard, `__main__.py` calls `main()`, and the unit's `ExecStart` is `python3 -m meteor_scatter.cli daemon` — but nothing asserts it. This is a real MUST gap. |
| **12.2 SSRC uniqueness (MUST)** | ✅ implemented. `_collect_issues` walks every `(freq, preset, sample_rate, encoding)` tuple per block and fails on a duplicate, naming both entries — because `MultiStream` keys its slot dict by SSRC and the second `add_channel()` would silently overwrite the first. |
| **12.3 Config-path disclosure (MUST)** | ✅ implemented. `build_validate` and `build_inventory` both emit the absolute `config_path` actually loaded. |
| **12.4 Decoder-spool mutation (SHOULD)** | ✅ documented, and structurally not a problem here: unlike `decode_ft8`, `jt9` does not unlink the WAV it decoded, so `SlotWorker`'s own `keep_wav` check is sufficient and `keep_wav = true` really does retain slots. [CONFIG.md](CONFIG.md) documents the flag next to the retention note. |
| **12.5 Pattern A layout (SHOULD)** | ✅ `install.sh` symlinks the checkout **into** `/opt/git/sigmond/meteor-scatter` (never the reverse), and smoke-tests that `meteorscat` can import the package — the traversability check the anti-pattern fails. |
| **12.6 ka9q-python PyPI lag (SHOULD)** | ⬜ not implemented. `validate` does not compare the installed `ka9q-python.__version__` against the declared minimum. |

Beyond the six, `validate` fails on: no `[[radiod]]` blocks, a block
with no `status`, and a `status` still set to the placeholder. It warns
on: empty callsign, empty grid square, no MSK144 frequencies, and an
unresolvable decoder override.

## §13 — Control surface (v0.5)

⛔ **Not implemented.** There is no
`/run/meteor-scatter/<instance>.control.sock`, and therefore no
`/healthz`, `/readyz`, `/status` or `/metrics`. `meteor-scatter status`
is a stub that prints "not running (Phase 1 not yet implemented)" and
exits 2 regardless of daemon state.

The live view available today is the journal (the 60 s per-mode stats
line and the per-cycle batcher commit line), `smd watch meteor`, and the
sink itself. `config show` / `config apply` (§14) are implemented and do
round-trip the config as JSON through the daemon's own validator.

## §14 — Configuration interview

✅ Implemented in `configurator.py`, registered in `deploy.toml
[contract.config]` as `meteor-scatter config init` / `config edit` so
sigmond drives the client's own interview rather than editing TOML.
A whiptail wizard (`scripts/config-wizard.sh`, driven by
`config/help.toml`) with a stdin-prompt fallback when whiptail is absent
or stdout is not a TTY; `--non-interactive` renders from the §14.3 env
bag (`STATION_CALL`, `STATION_GRID`, `SIGMOND_INSTANCE`,
`SIGMOND_RADIOD_STATUS`, plus `SIGMOND_RADIOD_COUNT` / `_INDEX` for
multi-radiod hosts). `config show --json` / `config apply --json -`
are the machine entry points, validated and atomically written.

⚠ `env show` works but **`env apply` manages no keys**:
`_ENV_WRITABLE_KEYS` is the empty set, so every payload is rejected with
"this build manages no env knobs yet". Sigmond does not depend on it —
`instance.py` writes the per-instance env stub directly from
`deploy.toml [contract.instance_env]`. `config/help.toml` still
documents an `[env.*]` section (`METEOR_SCATTER_DELIVERY_PIPELINES`,
`_USE_HS_UPLOADER`) whose variables no Python source reads — they live
only in `config/help.toml` and `scripts/config-wizard.sh`, inherited
from psk-recorder — and the
wizard's **Delivery** section submits exactly those keys through
`env apply` — so that menu item always ends in the "env apply failed"
dialog. Edit `/etc/meteor-scatter/env/<instance>.env` by hand instead.

## §15 — Radiod channel contributions

N/A. meteor-scatter declares no `[[radiod.fragment]]` blocks: it creates
its channels dynamically through `ensure_channel()` rather than
contributing a `radiod@<id>.conf.d/` fragment, and it uses radiod's
stock `usb` preset unmodified.

## §16 — Independent data-source clients

N/A. meteor-scatter's input is radiod RTP, so it is a §6/§7 client, not
an independent-source one; it emits no `data_path` block.

## §17 — Output sinks

✅ Two `data_sinks` entries per instance, both `kind = "file"`:

| Target | `retention_days` | `mb_per_day` |
|---|---|---|
| `<spool_dir>/<radiod_id>` — the WAV spool | `0` (deleted after decode) | `0` |
| `<log_dir>` — the per-mode spot logs | `365` | `5` |

The SQLite sink is reached through `sigmond.hamsci_sink.Writer`, which
resolves its own backend from the environment; from the contract's point
of view it is sigmond's own store, not a client-declared sink.

The staged target is **`psk.spots`** —
`Writer.from_env(table="spots", mode="psk", schema_version=2)` — with
MSK144 carried as the per-row `mode="msk144"` value. That is
intentional: the wsprdaemon server unifies `ft8`/`ft4`/`msk144` in one
`psk.spots` table behind one PSKReporter forwarder, so one pipeline
delivers all three and the cycle-tar path carries MSK144 for free.
`schema_version=2` must match what hs-uploader's reader filters on, or
rows are silently treated as stale-schema and never ship.

## §18 — Timing authority and the RTP-default fallback

✅ Subscriber.  Every channel anchors through the suite-shared
`hamsci_dsp.timing.acquire_anchor_utc`, which applies hf-timestd's published
offset to the labels whenever `authority.json` is fresh.  `inventory` reports
`uses_timing_calibration = true`, and `timing_authority_applied` comes from
the block the daemon writes once a minute (`core/applied_state.py`) — the
§18.5 amendment of 2026-09-04 wants the field to describe the labels, not a
reading habit.

Two distinct uses of the authority, easy to conflate:

* **Anchoring** — `ChannelSink` reads hf-timestd's authority through
  `hamsci_dsp.timing.AuthorityReader` to obtain the dynamic RTP→UTC
  offset when it takes its one-time anchor. Falls back to the host wall
  clock when `channel_info` is unavailable.
* **Provenance** — `ChTailer` reads the authority once per chunk and
  stamps a `timing_authority` block (source / tier / σ / age, or
  `standalone_timing_authority()` when hf-timestd is absent or stale)
  onto every spot row.

What it does *not* do is gate or correct timing against the authority:
MSK144 spot times are T/R-slot quantized, so RTP-default is sufficient.
That is a decision (sigmond #36), not an open gap.

There is no `core/authority_reader.py` in this build despite what
`CLAUDE.md` says — the reader moved to the shared `hamsci_dsp.timing`.

## §19 — Per-reporter instance and `reporter_id` row tag

✅ Implemented. `config.extract_reporter_id()` reads `[instance]
reporter_id`; `cli` falls back to `STATION_REPORTER_ID` from
coordination.env, and warns loudly when neither is set, because the
last-resort radiod-hostname fallback misattributes spots all the way to
PSKReporter. Every row carries `reporter_id`, plus the legacy `instance`
field (= radiod_id, slated for removal in sigmond Phase 9) and
`rx_source` (`radiod:<status>`).

`config.resolve_config_path()` prefers `/etc/meteor-scatter/<instance>.toml`
and emits a one-line `DeprecationWarning` pointing at
`sudo smd instance migrate` when it has to fall back to the shared
legacy config.

## What sigmond promises in return

Install and upgrade through the client's own `install.sh`; unit
resolution from `deploy.toml`; the coordination env
(`STATION_*`, `SIGMOND_SQLITE_PATH`, `RADIOD_<id>_CHAIN_DELAY_NS`,
`CLIENT_LOG_LEVEL`); the shared SQLite sink and the hs-uploader egress;
lifecycle locking and start ordering (radiod first); log discovery; and
`smd watch meteor` / TUI surfaces from the `[client_features]`
declarations.

CPU isolation is one of those promises, and it **is** kept for this
client: `'meteor-scatter@.service': 'other'` is in `AFFINITY_UNITS`
(`sigmond/lib/sigmond/cpu.py`), added in sigmond `6a9fe3f` (2026-07-20)
after the unit was found running unconfined on B4-100's radiod HT pair.
`smd admin diag cpu-affinity --apply` therefore writes the standard
non-radiod drop-in for it, keeping jt9's decode bursts off the cores
radiod's cache-hit rate depends on. This satisfies `MTS-Q-008` in
[REQUIREMENTS.md](REQUIREMENTS.md) §7, which asked for the membership to
be verified — it is there. What is still missing is anything that would
*keep* it there: no test asserts that a client with a templated decoder
unit appears in the map.

## Known drift (as of this verification)

Product files that still describe the psk-recorder parent rather than
this client. None of them break anything today; all of them will mislead
the next reader.

| Where | What it says | Truth |
|---|---|---|
| `contract.py` `deps.git` | `ka9q-radio` provides "jt9 --msk144 decoder (bundled in-repo)" | jt9 is WSJT-X's, built from source to `/usr/local/bin` by sigmond; nothing is bundled |
| `deploy.toml [[deps.git]]` | `ft8_lib` → `decode_ft8`, `ftlib-pskreporter` → `pskreporter-sender` | neither is on meteor-scatter's runtime path |
| `config/help.toml` | `[paths].decoder_kind` offers `decode_ft8` vs `jt9`; `[radiod.ft8]`/`[radiod.ft4]` sections; `[env.METEOR_SCATTER_DELIVERY_PIPELINES]` etc. | one mode (`msk144`), one decoder kind (`jt9`); the `[env.*]` variables are read by no Python source, and `env apply` manages no keys |
| `config/*.toml.template` | jt9 "bundled in-repo at bin/decoders/…"; `jt9 --msk144 -p 15` in a comment | no `bin/decoders/` in the repo; the default `-p` is 30 |
| `install.sh` `_verify_jt9` | probes `bin/decoders/jt9-<arch>-v*` | always warns and falls through to PATH resolution |
| `deploy.sh` | `VENV_DIR=/opt/meteor-scatter/venv` | `install.sh` creates `/opt/git/sigmond/meteor-scatter/venv` |
| `core/wav.py` docstring | "for decode_ft8 input" | the same format jt9 consumes |
| `CLAUDE.md` | FT8/FT4 architecture diagram, `decode_ft8`, `[radiod.ft8]`/`[radiod.ft4]` schema, `core/authority_reader.py`, sink `msk144.spots` | see this page and [ARCHITECTURE.md](ARCHITECTURE.md) |
| `REQUIREMENTS.md` | `MSK144_TR_PERIOD_SEC = 15`, sink `msk144.spots`, bundled arch-resolved jt9 | 30 s default, `psk.spots`, PATH-resolved jt9 — the code moved after that doc was reconciled on 2026-06-25 |

These are product-file (not documentation) changes and are out of scope
for the docs program; they are recorded here so the next contributor
does not treat them as truth. `REQUIREMENTS.md` remains the formal
requirements register — read it for `MTS-*` requirement IDs and the gap
list, and read the ★ pages in [INDEX.md](INDEX.md) for current behaviour.

## Versioning

`contract.py` `CONTRACT_VERSION`, `deploy.toml
[package].contract_version` and the sigmond catalog's `contract` field
must be bumped together. Sigmond compares the client's declared version
to its own supported version and warns on a mismatch, so a stale
declaration is visible in `smd status` rather than silent.

[contract]: https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md
