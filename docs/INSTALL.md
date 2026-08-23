# Installation

> **Audience:** operator/contributor
> **Status:** current
> **Verified against:** meteor-scatter bac2116 on 2026-08-23 — code
> **Canonical for:** installing and upgrading meteor-scatter on a host

Production install on Linux with systemd. Tested on Debian 13.

Two supported paths:

* **Under sigmond** (the normal case) — `smd install meteor-scatter`.
  Sigmond clones the repo to `/opt/git/sigmond/meteor-scatter` and runs
  the client's own `scripts/install.sh`; the catalog entry
  (`etc/catalog.toml [client.meteor-scatter]`) is what points at it.
  meteor-scatter is a **core** client of the `dasi2` profile (it moved
  there from `optional` on 2026-08-08, because leaving it discretionary
  meant a fresh `smd bringup dasi2` did not install a service the
  station's topology already declared), so `smd bringup dasi2` installs
  it unprompted.
* **Standalone** — clone and run `scripts/install.sh` yourself. No
  sigmond required; the sink writer degrades to a clean no-op and the
  spot log remains the local artefact.

## Prerequisites

### External binaries (not pip-installable)

| Binary | Source | Install path |
|---|---|---|
| `jt9` (3.0.2) | [WSJT-X](https://sourceforge.net/projects/wsjt/), built from pinned source by sigmond's `_build_wsjtx_decoders` | `/usr/local/bin/jt9` |
| `radiod` | [ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio) | system package or built from source |

**No decoder binaries ship in this repo.** WSJT-X is GPLv3, so sigmond
builds jt9 (and `wsprd`) on the host from retained pinned source rather
than redistributing a modified binary, and installs to `/usr/local/bin`,
which leads `/usr/bin` — so the build shadows any Debian `wsjtx`
package. `install.sh` still probes for a bundled
`bin/decoders/jt9-<arch>-v*`, does not find one, and warns; the runtime
then resolves `jt9` from PATH, which is the intended outcome. Set
`[paths].decoder_jt9` only to override that resolution.

There is no `decode_ft8` and no `pskreporter-sender` on meteor-scatter's
runtime path. (`deploy.toml` still declares both under `[[deps.git]]` —
a leftover of the psk-recorder skeleton, harmless but untrue.)

### radiod must be running

meteor-scatter talks to `radiod` exclusively via `ka9q-python`. It
resolves the radiod's status mDNS name (e.g.
`sigma-rx888mk2-status.local`) to find the control multicast group and
provisions channels there. If `radiod@<id>.service` is not active,
meteor-scatter cannot start.

The 6 m channel (50.260 MHz) needs a receiver and radiod path that
actually covers 6 m; on an RX888 that is the same direct-sampling path
that serves 6 m FT8/FT4.

### Avahi / mDNS

`*.local` resolution must work for `meteorscat`, the service user:

```bash
avahi-resolve -n <radiod>-status.local
```

If that fails, fix Avahi or `nsswitch.conf` before going further.

## First-run install: `scripts/install.sh`

Run as root from a clone at `/opt/git/sigmond/meteor-scatter`:

```bash
sudo /opt/git/sigmond/meteor-scatter/scripts/install.sh
```

What it does, in the script's own order:

1. **Service user** (Phase 1) — creates `meteorscat:meteorscat` (system
   user, `/usr/sbin/nologin`, no home). Idempotent.
2. **uv** (Phase 1.4) — sources sigmond's `scripts/install/ensure_uv.sh`
   (with an inline fallback for the standalone case) so `uv` is on
   `PATH`.
3. **Sibling repos** (Phase 1.5) — for each of `callhash`,
   `hs-uploader` and `ka9q-python`, checks
   `/opt/git/sigmond/<name>/pyproject.toml`; if it is not there, searches
   `~<invoker>/<name>`, `~<invoker>/git/<name>` and `/opt/git/<name>`,
   and otherwise clones from `https://github.com/HamSCI/<name>`. This is
   what makes `[tool.uv.sources]`' `path = "../<name>"` resolve in the
   next steps.

   ⚠ **A sibling found in one of those alternate locations is `mv`'d,
   not copied.** If you keep `~/git/callhash` as your working checkout,
   running `install.sh` relocates it to `/opt/git/sigmond/callhash` and
   your home path stops existing. (It refuses rather than clobbering when
   the target exists and is non-empty.) `SUDO_USER` is what it uses to
   find your home, so `sudo ./scripts/install.sh` searches *your* home,
   not root's.
4. **Repo link** (Phase 2) — ensures `/opt/git/sigmond/meteor-scatter`
   points at your checkout, symlinking if you cloned elsewhere, then
   asserts `meteorscat` can read `src/meteor_scatter/__init__.py` — the
   Pattern A traversability defence (§12.5), which fails loudly on a
   repo under a mode-700 home.
5. **Optional `--pull`** — `git pull --ff-only` on the linked repo.
6. **Venv** — `uv venv /opt/git/sigmond/meteor-scatter/venv --python
   3.11 --seed`. Note the venv lives *inside* the checkout, not at
   `/opt/meteor-scatter/venv`.
7. **`uv sync`** — `UV_PROJECT_ENVIRONMENT=<venv> uv sync --project
   <repo> --frozen --no-dev`, which reads `pyproject.toml` + the
   committed `uv.lock` and resolves `[tool.uv.sources]` to the sibling
   checkouts from step 3 as **editable** installs. That editability is
   the point: a `git pull` of any sibling reaches this venv with no
   reinstall.
8. **sigmond** — when `/opt/git/sigmond/sigmond` exists, `uv pip install
   --python <venv>/bin/python3 -e` it (it is lazy-imported with a
   fallback, so it is not in `pyproject.toml`).
9. **Import smoke-test** — `sudo -u meteorscat <venv>/bin/python3 -c
   'import meteor_scatter'`.
10. **Decoder probe** (Phase 2.5) — `_verify_jt9`, described above;
    non-fatal.
11. **Config dir** (Phase 3) — creates `/etc/meteor-scatter` but writes
    **no** config. Pre-rendering a placeholder used to make `config init`
    refuse ("already exists, pass `--reconfig`") and made instance
    enablement create a phantom `@default`; the installer now just tells
    you to run `config init`.
12. **Spool/log dirs** (Phase 4) — `/var/lib/meteor-scatter` and
    `/var/log/meteor-scatter`, owned `meteorscat:meteorscat`.
13. **Systemd unit** (Phase 5) — installs `meteor-scatter@.service` to
    `/etc/systemd/system/`, then `daemon-reload`.

Two things it deliberately does **not** do:

* **It disables no native ka9q-radio service.** Unlike psk-recorder and
  wspr-recorder, MSK144 monitors its own dial frequencies and overlaps
  no `ft8-record` / `ft4-record` / `pskreporter@` unit.
* **It enables no instance.** Enablement follows configuration:
  `config init` enables `meteor-scatter@<id>` for the id(s) it writes.

Then configure and start:

```bash
smd config init meteor-scatter          # or: sudo meteor-scatter config init
smd start --components meteor-scatter   # or: sudo systemctl start meteor-scatter@<instance>
```

## Ongoing deploys: `scripts/deploy.sh`

For developer iteration after the initial install:

```bash
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh              # refresh + restart
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh --pull       # git pull --ff-only first
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh --no-restart # install only
sudo /opt/git/sigmond/meteor-scatter/scripts/deploy.sh --force-dirty
```

It verifies the venv exists, verifies a clean git tree unless
`--force-dirty`, optionally pulls, refreshes the editable install,
re-installs the unit file + `daemon-reload`, and unless `--no-restart`
restarts every running `meteor-scatter@*` instance. It creates no users,
touches no native ka9q-radio services, and never overwrites config.

⚠ `deploy.sh` still carries `VENV_DIR="/opt/meteor-scatter/venv"` while
`install.sh` creates `/opt/git/sigmond/meteor-scatter/venv`. On a host
installed by `install.sh` the deploy script's venv check will not find
what it expects — prefer re-running `install.sh` (it is idempotent, and
`uv sync` is the only thing that re-resolves the editable siblings).

Because the siblings are editable installs, the routine fleet upgrade is
not `install.sh` at all:

```bash
smd component update      # git pull per topology version policy
smd restart               # reload the new code into memory
```

## Multiple radiods on one host

Each `[[radiod]]` block corresponds to one systemd instance:

```toml
[[radiod]]
status = "sigma-rx888mk2-status.local"
[radiod.msk144]
tr_period_sec = 30
freqs_hz = [28_145_000, 50_260_000]

[[radiod]]
status = "bee3-status.local"
[radiod.msk144]
freqs_hz = [28_145_000]
```

```bash
sudo systemctl enable --now meteor-scatter@<instance-a>
sudo systemctl enable --now meteor-scatter@<instance-b>
```

Instances are independent — one failing does not affect the other. A
single daemon *can* also drive several sources in one process
(`MeteorScatterRecorder` holds a `ReceiverManager` per source), which is
the multi-source path wspr-recorder pioneered; the unit's
`--radiod-id %i` selects the single-source legacy behaviour.

## File and path layout

| Path | Purpose | Owner |
|---|---|---|
| `/etc/meteor-scatter/<instance>.toml` | Per-instance config (preferred) | root, mode 644 |
| `/etc/meteor-scatter/meteor-scatter-config.toml` | Legacy shared config (deprecated fallback) | root |
| `/etc/meteor-scatter/env/<instance>.env` | Per-instance env (`METEOR_SCATTER_DELIVERY_MODE`, …) | root |
| `/etc/sigmond/coordination.env` | Sigmond cross-client coordination env | root |
| `/etc/systemd/system/meteor-scatter@.service` | Templated unit | root |
| `/opt/git/sigmond/meteor-scatter/` | Source checkout (editable install root) | repo owner; readable by `meteorscat` |
| `/opt/git/sigmond/meteor-scatter/venv/` | Python venv | root |
| `/usr/local/bin/meteor-scatter` | CLI symlink into the venv | root |
| `/var/lib/meteor-scatter/<radiod_id>/msk144/` | WAV spool (deleted after decode unless `keep_wav`) | `meteorscat` |
| `/var/log/meteor-scatter/<radiod_id>-msk144.log` | Spot log (`SlotWorker` appends, `ChTailer` reads) | `meteorscat` |
| `/var/lib/sigmond/sink.db` | Shared `psk.spots` sink | `sigmond` group |
| `/var/lib/hs-uploader/watermarks.db` | Upload watermark/retry state, **shared** across client users | `root:sigmond`, mode 02775 via tmpfiles.d |

The process log goes to the systemd journal
(`StandardOutput=journal`), not a file — `journalctl -u
meteor-scatter@<instance>` or `smd log meteor-scatter`. Only the
file-based spot logs appear in `inventory --json` `log_paths`.

⚠ Never `chown` `/var/lib/hs-uploader`. Its setgid group-write shape is
owned by `tmpfiles.d/hs-uploader.conf`; an earlier version of this unit
re-stomped the group to `meteorscat` on every restart and locked
wsprdaemon-client out of the shared `watermarks.db` with "attempt to
write a readonly database" (observed on bee1, 2026-05-14).

## Uninstall

Under sigmond, `smd remove meteor-scatter` drops the topology section
and stops the units while preserving the checkout; a full purge is
`smd remove --purge --force meteor-scatter` (the `--force` guard exists
because meteor-scatter is not a deprecated name). By hand:

```bash
sudo systemctl disable --now 'meteor-scatter@*'
sudo rm /etc/systemd/system/meteor-scatter@.service
sudo systemctl daemon-reload
sudo rm /usr/local/bin/meteor-scatter
sudo rm -rf /etc/meteor-scatter /var/lib/meteor-scatter /var/log/meteor-scatter
sudo userdel meteorscat
```

The checkout at `/opt/git/sigmond/meteor-scatter`, `radiod`, and
`/usr/local/bin/jt9` (shared with wspr-recorder and psk-recorder) are
untouched.
