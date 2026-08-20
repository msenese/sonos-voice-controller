#!/bin/bash
# Self-heals edge-impulse-linux-runner's local --monitor cache after a crash
# or hard power-cycle. See docs/model-monitoring-corruption.md for the full
# writeup of why this exists.
#
# Root cause: --monitor buffers local inference data as individual JSON
# files under ~/.ei-linux-runner/storage/<project>/<model-version>/ before
# uploading batches to Edge Impulse's cloud, tracked against an internal
# index of which numbered subdirectories ("buckets") it expects to exist.
# Two confirmed ways this cache goes bad and crashes the whole runner
# process (not just monitoring) on next start:
#   1. A file killed mid-write (SIGTERM from a restart, or a hard power
#      loss) is left at 0 bytes; the runner tries to JSON.parse("") without
#      checking it's non-empty, and that throws uncaught.
#   2. The on-disk buckets and the runner's internal index of them get out
#      of sync -- confirmed live 2026-08-19 after several SD-card image
#      swaps left an older on-disk snapshot with fewer buckets than the
#      index expected -- and it crashes with ENOENT trying to scandir a
#      bucket directory that doesn't exist.
# Restart=always then just relaunches into the same broken state forever,
# until a human intervenes -- this took down live voice control, not just
# monitoring, both times.
#
# Rather than keep chasing individual corruption shapes as new ones turn
# up, startup mode wipes the whole per-project cache unconditionally. This
# is safe specifically at startup because nothing is running yet -- it's
# pending-upload staging data Edge Impulse manages, not project data this
# repo owns -- so losing whatever hadn't uploaded yet is a trivial cost
# next to the crash loop it prevents.
#
# Usage:
#   ei-runner-cache-cleanup.sh            -- startup mode: delete the whole
#     per-project cache. Unconditional wipe, not just zero-byte files --
#     covers corruption shapes we haven't seen yet too, not just the two
#     above.
#   ei-runner-cache-cleanup.sh --running  -- periodic mode: only remove
#     zero-byte files older than 5 minutes. Can't safely wipe everything
#     here since the runner IS actively using the directory -- this only
#     catches corruption case #1 mid-session; case #2 needs a restart
#     (startup mode) to clear, since it wasn't observed to occur while
#     already running.

STORAGE_ROOT="/home/msenese/.ei-linux-runner/storage"

if [ ! -d "$STORAGE_ROOT" ]; then
    exit 0
fi

if [ "$1" = "--running" ]; then
    COUNT=$(find "$STORAGE_ROOT" -type f -iname '*.json' -size 0 -mmin +5 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        echo "ei-runner-cache-cleanup: removing $COUNT corrupted (zero-byte) monitoring cache file(s)"
        find "$STORAGE_ROOT" -type f -iname '*.json' -size 0 -mmin +5 -delete
    fi
else
    PROJECT_DIRS=$(find "$STORAGE_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    if [ -n "$PROJECT_DIRS" ]; then
        echo "ei-runner-cache-cleanup: startup -- wiping monitoring cache (pending-upload staging data only, safe to lose): $PROJECT_DIRS"
        # This runs as ExecStartPre -- a non-zero exit here would stop
        # systemd from starting ei-runner.service at all, which would be a
        # worse failure than the crash-loop this script exists to prevent.
        # `|| true` so a partial rm failure (confirmed possible if this is
        # ever invoked while the old process hasn't fully stopped yet, e.g.
        # a manual run) can't block the service from starting.
        rm -rf $PROJECT_DIRS || true
    fi
fi

exit 0
