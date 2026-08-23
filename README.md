# meteor-scatter

Meteor-scatter **ping recorder and decoder** for [ka9q-radio][ka9q].
Receives WSJT-X **MSK144** monitoring channels from `radiod` via
[ka9q-python][ka9qpy], records T/R-aligned slots, decodes each with
`jt9 --msk144`, and stages the resulting spots into the HamSCI sigmond
suite's shared spot sink. Follows the sigmond [client contract][contract]
(v0.8).

Meteor trails ionize for milliseconds to a couple of seconds, opening
ultra-short propagation windows. MSK144 — WSJT-X's FEC'd successor to
FSK441 — packs 72 ms frames with aggressive LDPC coding so that a single
ping can carry a whole decode. This client monitors the conventional
MSK144 channels (10 m @ 28.145 MHz, 6 m @ 50.260 MHz) and reports every
ping it hears. It is passive monitoring, not QSO operation.

```
radiod (ka9q-radio)
  │   RTP multicast, one stream per MSK144 channel (usb, 12 kHz, s16be)
  ▼
meteor-scatter daemon (one per radiod)
  ├─ per-channel: RTP-anchored SlotClock → ring → T/R slot WAV
  │               → fork `jt9 -Y --msk144 -p <tr> -f 1500 -a <wd>`
  │               → read the delta appended to jt9's decoded.txt
  ├─ per-radiod:  <radiod_id>-msk144.log   (normalized decode lines)
  └─ ChTailer → callhash resolution → cycle batcher
       → sigmond.hamsci_sink.Writer → psk.spots (per-row mode="msk144")
            └─ hs-uploader → pskreporter.info   (direct mode only)
```

Two details that are easy to assume wrong:

* **The sink target is `psk.spots`, not `msk144.spots`.** MSK144 is a
  value of the per-row `mode` column in the same table psk-recorder's
  FT8/FT4 rows use, so one PSKReporter pipeline
  (`mode IN (ft8, ft4, msk144)`) delivers all three.
* **The default T/R period is 30 s**, matching a stock WSJT-X MSK144
  install. `[radiod.msk144].tr_period_sec` overrides it (WSJT-X also
  offers 5/10/15 s; 15 s is the 2 m VHF meteor-ping convention). That one
  number is the slot length, the decode cadence and `jt9 -p`.

Compound callsigns are recovered through the shared [callhash][callhash]
library: `jt9 -Y` emits an unresolved compound call as a numeric 22-bit
`<NNNNNNN>` hash, and the cross-mode table substitutes the plaintext back
from accumulated sightings — so a call learned on FT8 or WSPR resolves an
MSK144 hash and vice-versa.

One `meteor-scatter@<instance>.service` per radiod; each instance handles
every MSK144 frequency configured on that radiod.

## Quickstart

Prerequisites:
- A working `radiod@<id>.service` from [ka9q/ka9q-radio][ka9q], reachable
  by its mDNS status name, covering the bands you want (6 m included).
- `jt9` (WSJT-X 3.0.2) at `/usr/local/bin/jt9`. On a sigmond host
  `smd` builds it from pinned source; **no decoder binaries ship in this
  repo**.

Then:

```bash
git clone https://github.com/HamSCI/meteor-scatter /opt/git/sigmond/meteor-scatter
sudo /opt/git/sigmond/meteor-scatter/scripts/install.sh   # user, venv (uv), dirs, unit
sudo meteor-scatter config init                           # interactive wizard -- see below
sudo systemctl start meteor-scatter@<instance>
journalctl -fu meteor-scatter@<instance>
```

Under sigmond, `smd install meteor-scatter` runs that same `install.sh`,
and `smd config init meteor-scatter` / `smd start --components
meteor-scatter` replace the last two steps. `install.sh` deliberately
writes no config and enables no instance — configuration comes first.

⛔ Restarting meteor-scatter through `smd` bounces radiod for the whole
station (it declares `requires = ["ka9q-python", "ka9q-radio"]`). See
[docs/OPERATIONS.md](docs/OPERATIONS.md) before you restart anything.

### Configuration

meteor-scatter's operator-facing config spans **three persistence layers**:

| Layer | Path | Owner | Holds |
|---|---|---|---|
| **TOML config** | `/etc/meteor-scatter/<instance>.toml` (legacy: `meteor-scatter-config.toml`) | meteor-scatter | `[station]`, `[paths]`, `[processing]`, `[timing]`, `[instance]`, `[[radiod]]` + `[radiod.msk144]` |
| **Coordination env** | `/etc/sigmond/coordination.env` | sigmond | `STATION_CALL`, `STATION_GRID`, `STATION_REPORTER_ID`, `SIGMOND_SQLITE_PATH`, `RADIOD_<id>_CHAIN_DELAY_NS` |
| **Per-instance env** | `/etc/meteor-scatter/env/<instance>.env` | sigmond seeds it; edit by hand | `METEOR_SCATTER_DELIVERY_MODE`, `METEOR_SCATTER_DIRECT_DEDUP`, `METEOR_SCATTER_LOG_LEVEL` |

The wizard manages layer 1; it reads layer 2 for pre-fills and never
writes there. Layer 3 is seeded by sigmond from `deploy.toml
[contract.instance_env]` (`METEOR_SCATTER_DELIVERY_MODE = "deposit"` on a
host whose hs-uploader daemon owns egress).

⚠ `meteor-scatter env apply` manages **no** keys in this build
(`_ENV_WRITABLE_KEYS` is empty), so the wizard's *Delivery* menu item
fails and layer 3 must be edited directly. `env show` works.

#### Interactive wizard (default)

When stdout is a TTY and `whiptail` is installed, `meteor-scatter config
init` (first time) and `config edit` (subsequent) launch a menu-driven
wizard:

```
Station    Call=AC0G  Grid=EM38ww40pk
Paths      spool=/var/lib/meteor-scatter  decoder=jt9
Processing lifetime=6000 frames
Timing     chain_delay=0 ns (sigmond usually overrides)
Radiod     blocks: sigma-rx888mk2-status.local
Delivery   pipelines: ... (per-instance env — see the warning above)
Edit-TOML  Open raw config in $EDITOR (for freqs_hz lists)
Apply      Review and write changes
Cancel     Discard pending changes and exit
```

Cancel inside a section drops back to the menu — effective "back"
navigation. Each section walks its questions linearly with per-field help
and validation.

- **Station / Paths / Processing / Timing** edit the TOML through
  `config apply`.
- **Radiod** picks an existing `[[radiod]]` block to edit (its `status`
  mDNS name) or adds a new one. `freqs_hz` arrays stay in raw TOML — use
  **Edit-TOML** for those.

Per-key help lives in `config/help.toml`; pre-fills come from
`/etc/sigmond/coordination.env` and the current TOML.

#### Headless / scripted

```bash
meteor-scatter config init --non-interactive
```

Renders the template with `STATION_CALL` / `STATION_GRID` /
`SIGMOND_INSTANCE` / `SIGMOND_RADIOD_STATUS` env-bag substitutions, no
prompts.

#### Hand-edit

```bash
sudoedit /etc/meteor-scatter/<instance>.toml
sudoedit /etc/meteor-scatter/env/<instance>.env
```

Prefer this if you value the template's inline comments — `config apply`
rewrites the TOML cleanly and does not preserve them.

#### JSON entry points (for sigmond / other tooling)

```bash
meteor-scatter config show  --json [--defaults]              # → TOML as JSON
meteor-scatter config apply --json -                         # ← stdin JSON, validated, atomic write
meteor-scatter env    show  --json --instance <instance>     # → env file as JSON
meteor-scatter inventory --json                              # contract v0.8 resource view
meteor-scatter validate  --json                              # contract v0.8 validation
meteor-scatter version   --json
```

`config apply` writes `[station]`, `[paths]`, `[processing]`, `[timing]`
and `[[radiod]]` (overlay-wins for the radiod list — pass `freqs_hz` back
in the payload to preserve it).

For ongoing development on a checked-out repo:

```bash
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh         # refresh + restart
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh --pull  # git pull then deploy
```

For tests (no venv needed):

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
uv sync --extra dev && uv run pytest tests/ -v    # canonical
```

## Documentation

Start at [docs/INDEX.md](docs/INDEX.md).

- [docs/INSTALL.md](docs/INSTALL.md) — install and upgrade: deps, multi-radiod, paths, permissions
- [docs/CONFIG.md](docs/CONFIG.md) — every TOML key and environment variable
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — running it: control, logs, health signs, failures
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — internals for contributors
- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — formal requirements register (`MTS-*`)
- [docs/SIGMOND-CONTRACT.md](docs/SIGMOND-CONTRACT.md) — section-by-section contract conformance
- [CLAUDE.md](CLAUDE.md) — development briefing

## What it does and does not

**Does:** receive RTP multicast from `radiod`; anchor a slot clock once
off radiod's GPS-true RTP timestamp and defer to RTP thereafter;
slot-align audio to the configured MSK144 T/R period; write one mono
12 kHz WAV per slot; fork `jt9 --msk144` and read its `decoded.txt`
delta; normalize each decode into a per-radiod spot log; resolve
compound-call hashes through the shared callhash table; stamp timing
provenance and reporter identity on every row; stage rows into
`psk.spots`; and — in `direct` delivery mode — POST them to
pskreporter.info through hs-uploader's `PskReporterTcp` transport.

**Does not:** decode FT8 or FT4 (that is [psk-recorder][psk]);
reimplement the MSK144 decoder (it shells out to WSJT-X's `jt9`);
reimplement the PSKReporter protocol (hs-uploader owns the socket);
transmit anything; or talk to `radiod` over anything but
[ka9q-python][ka9qpy]. Multicast destination addresses are *resolved
from* radiod, never specified by meteor-scatter.

## License

MIT. See [LICENSE](LICENSE). Author: Michael Hauan, AC0G.
`jt9` is WSJT-X, GPLv3, built from source on the host — not
redistributed here.

[ka9q]: https://github.com/ka9q/ka9q-radio
[ka9qpy]: https://github.com/HamSCI/ka9q-python
[callhash]: https://github.com/HamSCI/callhash
[psk]: https://github.com/HamSCI/psk-recorder
[contract]: https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md
