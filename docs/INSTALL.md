# Installation

Production install on Linux with systemd. Tested on Debian 13.

## Prerequisites

### External binaries (not pip-installable)

| Binary | Source | Install path |
|---|---|---|
| `decode_ft8` | [ka9q/ft8_lib](https://github.com/ka9q/ft8_lib) | `/usr/local/bin/decode_ft8` |
| `pskreporter-sender` | [pjsg/ftlib-pskreporter](https://github.com/pjsg/ftlib-pskreporter) | `/usr/local/bin/pskreporter-sender` (or in a shared venv) |
| `radiod` | [ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio) | system package or built from source |

The decoder and uploader paths are configurable; see [CONFIG.md](CONFIG.md).

### radiod must be running

msk144-recorder talks to `radiod` exclusively via `ka9q-python`. It
resolves the radiod's status mDNS hostname (e.g. `bee1-status.local`)
to find the control multicast group, and provisions channels there. If
`radiod@<id>.service` is not active, msk144-recorder cannot start.

### Avahi / mDNS

Resolution of `*.local` hostnames must work for `msk144rec` (the service
user). Verify with:

```bash
avahi-resolve -n bee1-status.local
```

If this fails, fix Avahi or `nsswitch.conf` before going further.

## First-run install: `scripts/install.sh`

Run as root from a clone at `/opt/git/sigmond/msk144-recorder`:

```bash
sudo /opt/git/sigmond/msk144-recorder/scripts/install.sh
```

What it does, in order:

1. **Service user** — creates `msk144rec:msk144rec` (system user, nologin
   shell, no home directory). Idempotent.
2. **Repo link** — ensures `/opt/git/sigmond/msk144-recorder` is reachable; if
   you cloned elsewhere, symlinks it. Verifies `msk144rec` can `cat`
   `src/msk144_recorder/__init__.py` (catches permission/traversability
   bugs early).
3. **Optional `--pull`** — `git pull --ff-only` on the repo before
   installing.
4. **Venv** — `/opt/msk144-recorder/venv` (Python ≥ 3.10), pip/setuptools/wheel
   upgraded.
5. **Editable install** — `pip install -e /opt/git/sigmond/msk144-recorder`.
   Source edits in the repo take effect on next service restart, no
   reinstall needed.
6. **Import smoke-test** — `sudo -u msk144rec python3 -c 'import msk144_recorder'`.
7. **Config template** — copies `config/msk144-recorder-config.toml.template`
   to `/etc/msk144-recorder/msk144-recorder-config.toml` if absent. Existing
   config is never overwritten.
8. **Spool/log dirs** — `/var/lib/msk144-recorder` and
   `/var/log/msk144-recorder`, owned by `msk144rec:msk144rec`.
9. **Systemd unit** — installs `msk144-recorder@.service` to
   `/etc/systemd/system/`, runs `daemon-reload`.
10. **Disables ka9q-radio's native units** — `ft8-record.service`,
    `ft4-record.service`, `ft8-decode@*.service`, `ft4-decode@*.service`,
    `pskreporter@ft{4,8}.service`. These are mutually exclusive with
    msk144-recorder.
11. **Enables instances** — for each `[[radiod]].id` in the rendered
    config, `systemctl enable msk144-recorder@<id>.service`. **Does not
    start** — that's a deliberate choice so you can edit config first.

After install, edit the config (see [CONFIG.md](CONFIG.md)), then:

```bash
sudo systemctl start msk144-recorder@<radiod_id>
```

## Ongoing deploys: `scripts/deploy.sh`

For developer iteration after the initial install:

```bash
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh           # refresh editable install + restart
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh --pull    # git pull --ff-only first
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh --no-restart   # install only, don't bounce instances
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh --force-dirty  # allow uncommitted changes
```

What it does:

1. Verifies the venv exists (errors if not — use `install.sh` first).
2. Verifies clean git tree unless `--force-dirty`.
3. Optional `--pull`.
4. `pip install -e /opt/git/sigmond/msk144-recorder` (refresh in case
   `pyproject.toml` changed).
5. Updates the systemd unit file (`install -m 644`).
6. Unless `--no-restart`, restarts every enabled `msk144-recorder@*` instance.

Does **not** create users, touch native ka9q-radio services, or
overwrite config.

## Multiple radiods on one host

Each `[[radiod]]` block in the config corresponds to one instance:

```toml
[[radiod]]
id            = "bee1-rx888"
radiod_status = "bee1-status.local"
[radiod.ft8]
freqs_hz = [...]

[[radiod]]
id            = "bee3-rx888"
radiod_status = "bee3-status.local"
[radiod.ft8]
freqs_hz = [...]
```

Each becomes a separate systemd instance:

```bash
sudo systemctl enable --now msk144-recorder@bee1-rx888
sudo systemctl enable --now msk144-recorder@bee3-rx888
```

Instances are independent — failure of one does not affect the other.

## File and path layout

| Path | Purpose | Owner |
|---|---|---|
| `/etc/msk144-recorder/msk144-recorder-config.toml` | Config (operator-edited) | root, mode 644 |
| `/etc/msk144-recorder/env/<id>.env` | Optional per-instance env (sigmond-managed) | root |
| `/etc/sigmond/coordination.env` | Sigmond cross-client coordination env | root |
| `/etc/systemd/system/msk144-recorder@.service` | Templated unit | root |
| `/opt/git/sigmond/msk144-recorder/` | Source checkout (editable install root) | repo owner; readable by `msk144rec` |
| `/opt/msk144-recorder/venv/` | Python venv | root |
| `/var/lib/msk144-recorder/<radiod_id>/{ft8,ft4}/` | WAV spool (deleted after decode unless `keep_wav = true`) | `msk144rec` |
| `/var/log/msk144-recorder/<radiod_id>-{ft8,ft4}.log` | Per-mode spot log (decoder appends, sender tails) | `msk144rec` |

The process log goes to the systemd journal (`StandardOutput=journal`),
not a file — query it with `journalctl -u msk144-recorder@<radiod_id>`.

## Uninstall

There is no uninstall script; remove by hand if needed:

```bash
sudo systemctl disable --now 'msk144-recorder@*'
sudo rm /etc/systemd/system/msk144-recorder@.service
sudo systemctl daemon-reload
sudo rm -rf /opt/msk144-recorder /etc/msk144-recorder /var/lib/msk144-recorder /var/log/msk144-recorder
sudo userdel msk144rec
```

The repo at `/opt/git/sigmond/msk144-recorder` and the external binaries
(`decode_ft8`, `pskreporter-sender`, `radiod`) are untouched.
