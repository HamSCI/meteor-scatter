#!/bin/bash
# deploy.sh — Pattern A editable-install refresh for meteor-scatter
#
# Usage: sudo ./scripts/deploy.sh [--pull] [--no-restart]
#
# What it does:
#   1. Verifies clean git tree (unless --force-dirty)
#   2. Optional git pull --ff-only
#   3. Traversability check: service user can read repo
#   4. pip install -e . (refresh editable install)
#   5. Restarts enabled meteor-scatter@* instances
#
# Does NOT:
#   - Create service user or venv (use install.sh for first-run)
#   - Touch native ka9q-radio services (only install.sh does that)
#   - Overwrite config

set -euo pipefail

SERVICE_USER="meteorscat"
REPO_SOURCE="/opt/git/sigmond/meteor-scatter"
VENV_DIR="/opt/meteor-scatter/venv"

ui_info()  { echo "[INFO]  $*"; }
ui_warn()  { echo "[WARN]  $*" >&2; }
ui_error() { echo "[ERROR] $*" >&2; }

DO_PULL=false
DO_RESTART=true
FORCE_DIRTY=false
for arg in "$@"; do
    case "$arg" in
        --pull)        DO_PULL=true ;;
        --no-restart)  DO_RESTART=false ;;
        --force-dirty) FORCE_DIRTY=true ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    ui_error "Must run as root (sudo)"
    exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
    ui_error "Venv not found at $VENV_DIR — run install.sh first"
    exit 1
fi

# Step 1: clean tree check
if ! $FORCE_DIRTY; then
    if [[ -n "$(git -C "$REPO_SOURCE" status --porcelain)" ]]; then
        ui_error "Repo has uncommitted changes. Commit first or use --force-dirty"
        exit 1
    fi
fi

# Step 2: optional pull
if $DO_PULL; then
    ui_info "Pulling latest from origin"
    git -C "$REPO_SOURCE" pull --ff-only
fi

# Step 3: traversability check
if ! sudo -u "$SERVICE_USER" test -r "$REPO_SOURCE/src/meteor_scatter/__init__.py"; then
    ui_error "Service user $SERVICE_USER cannot read $REPO_SOURCE"
    exit 1
fi

# Step 4: editable install refresh
ui_info "Refreshing editable install"
"$VENV_DIR/bin/pip" install -e "$REPO_SOURCE" >/dev/null

# Post-install verify
if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c 'import meteor_scatter' 2>/dev/null; then
    ui_error "Post-install verify failed"
    exit 1
fi
ui_info "Post-install verify OK"

# Step 5: install unit file (in case it changed)
install -o root -g root -m 644 \
    "$REPO_SOURCE/systemd/meteor-scatter@.service" \
    /etc/systemd/system/meteor-scatter@.service
systemctl daemon-reload

# Step 6: restart instances
if $DO_RESTART; then
    INSTANCES=$(systemctl list-units --plain --no-legend 'meteor-scatter@*.service' 2>/dev/null | awk '{print $1}')
    if [[ -n "$INSTANCES" ]]; then
        for unit in $INSTANCES; do
            ui_info "Restarting $unit"
            systemctl restart "$unit"
        done
    else
        ui_info "No meteor-scatter instances currently running"
    fi
fi

ui_info "Deploy complete"
