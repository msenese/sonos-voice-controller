#!/bin/bash
# Quick, incremental backup of everything on the live Pi that ISN'T already
# version-controlled in this repo: secrets (config.py, the EI CLI's cached
# login), trained model files, sample data, and the actually-deployed (not
# just templated) systemd units + sudoers rule -- the exact stuff that cost
# a full afternoon to reconstruct after the 16GB card died.
#
# Run by hand after a session with real changes to the Pi (not scheduled --
# by design, see the decision in chat). Backed by rsync, so the first run
# moves real data but every run after that is fast, only transferring
# what's changed.
set -e
cd "$(dirname "$0")"

PI_HOST="msenese@192.168.50.99"
SSH_KEY="$HOME/.ssh/sonos_pi_session"
DEST="pi-backups/live-files"

mkdir -p "$DEST/systemd"

echo "=== Syncing home-directory files ==="
rsync -avz -e "ssh -i $SSH_KEY" \
    "$PI_HOST:~/config.py" \
    "$PI_HOST:~/edge-impulse-config.json" \
    "$PI_HOST:~/sonos-model.eim" \
    "$PI_HOST:~/sonos-model-previous.eim" \
    "$PI_HOST:~/sonos-model.meta.json" \
    "$PI_HOST:~/sonos-model-previous.meta.json" \
    "$PI_HOST:~/sample_counts_baseline.json" \
    "$DEST/" || echo "  (one or more files above don't exist on the Pi right now -- fine, continuing)"

echo "=== Syncing directories (sample/training data) ==="
for dir in training_samples models noise-samples trigger-captures; do
    rsync -avz -e "ssh -i $SSH_KEY" "$PI_HOST:~/$dir/" "$DEST/$dir/" 2>/dev/null \
        && echo "  synced $dir" \
        || echo "  skipped $dir (doesn't exist on the Pi right now)"
done

# Includes the deploy key (sonos_deploy_key) used for git-archive pushes --
# recovering that key beats re-registering a new deploy key on GitHub.
echo "=== Syncing .ssh (host keys, deploy key, known_hosts) ==="
rsync -avz -e "ssh -i $SSH_KEY" "$PI_HOST:~/.ssh/" "$DEST/.ssh/"

echo "=== Syncing actually-deployed systemd units ==="
rsync -avz -e "ssh -i $SSH_KEY" \
    "$PI_HOST:/etc/systemd/system/ei-runner.service" \
    "$PI_HOST:/etc/systemd/system/sonos-controller.service" \
    "$PI_HOST:/etc/systemd/system/sonos-dashboard.service" \
    "$DEST/systemd/"

echo "=== Syncing sudoers rule (needs sudo on the Pi) ==="
ssh -i "$SSH_KEY" "$PI_HOST" "sudo cat /etc/sudoers.d/sonos-dashboard" > "$DEST/sudoers.d-sonos-dashboard"

echo
echo "=== Done ==="
du -sh "$DEST"
