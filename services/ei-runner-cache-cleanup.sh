#!/bin/bash
# Self-heals edge-impulse-linux-runner's local --monitor cache after a crash
# or hard power-cycle. See docs/model-monitoring-corruption.md for the full
# writeup of why this exists.
#
# Root cause: --monitor buffers local inference data as individual JSON
# files under ~/.ei-linux-runner/storage/<project>/<model-version>/ before
# uploading batches to Edge Impulse's cloud. If the process is killed while
# a file is mid-write (SIGTERM from a restart, or a hard power loss), it's
# left at 0 bytes. On next start, the runner tries to parse every file in
# that cache without checking it's non-empty first -- JSON.parse("") throws
# "Unexpected end of JSON input", which crashes the whole process, not just
# the monitoring subsystem. Restart=always then just relaunches into the
# same broken state, forever, until a human removes the bad file(s).
#
# Usage:
#   ei-runner-cache-cleanup.sh            -- startup mode: remove all
#     zero-byte JSON files immediately. Safe with no age check because
#     nothing is running yet -- anything 0 bytes here is unambiguously
#     stale from a previous crashed run, not an in-progress write.
#   ei-runner-cache-cleanup.sh --running  -- periodic mode: only remove
#     zero-byte files older than 5 minutes, since the runner IS active and
#     a freshly-created 0-byte file might just be mid-write, not corrupted.

STORAGE_ROOT="/home/msenese/.ei-linux-runner/storage"

if [ ! -d "$STORAGE_ROOT" ]; then
    exit 0
fi

if [ "$1" = "--running" ]; then
    MIN_AGE="-mmin +5"
else
    MIN_AGE=""
fi

COUNT=$(find "$STORAGE_ROOT" -type f -iname '*.json' -size 0 $MIN_AGE 2>/dev/null | wc -l)
if [ "$COUNT" -gt 0 ]; then
    echo "ei-runner-cache-cleanup: removing $COUNT corrupted (zero-byte) monitoring cache file(s)"
    find "$STORAGE_ROOT" -type f -iname '*.json' -size 0 $MIN_AGE -delete
fi
