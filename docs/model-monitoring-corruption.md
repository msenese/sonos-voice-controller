# Model Monitoring cache corruption: what happens, why, and the fix

## Summary

`ei-runner.service` runs `edge-impulse-linux-runner` with `--monitor`
(Edge Impulse's Model Monitoring feature: continuous local inference +
periodic summary upload to Studio). When the runner process is terminated
abruptly -- a `systemctl restart`, or a hard power-cycle of the Pi -- it can
leave a zero-byte JSON file behind in its local monitoring cache. On the
next startup, the runner tries to parse every file in that cache and
crashes on the empty one with `Unexpected end of JSON input`. Because
`Restart=always` just relaunches into the exact same broken state, this
becomes an infinite crash loop -- the voice pipeline goes down (the
`sonos-controller.py` LED shows a red "can't reach the classifier" flash)
until a human manually removes the corrupted file(s).

**This takes down the actual voice control feature, not just monitoring.**
The crash is in the runner process itself, which is the same process that
serves live classification to `sonos-controller.py`.

## Where the corrupted files live

```
~/.ei-linux-runner/storage/<project-id>/<model-version>/
```

e.g. `~/.ei-linux-runner/storage/1032165/28/metrics/5867_6086.json`. This
directory is Edge Impulse's own local staging area for monitoring data
pending upload -- not something this project created or manages directly.

## Root cause (best understanding, not officially confirmed by Edge Impulse)

`--monitor` buffers local inference summaries as individual JSON files
before batch-uploading them to Studio. Writing a file is presumably not
atomic (not written to a temp path and renamed into place) -- so a process
killed mid-write leaves a real, 0-byte file sitting where a complete one
was expected. On the next start, the runner appears to try to read/resume
every file in this cache without first checking it's non-empty or
well-formed, so `JSON.parse("")` throws, and that exception isn't caught --
it crashes the whole process rather than just skipping the one bad file or
disabling monitoring for that run.

This is a robustness gap in the `edge-impulse-linux` npm package itself
(tested version: `edge-impulse-linux-runner v1.25.1`), not a bug in this
project's code. It's a realistic failure mode for any always-on embedded
device: Raspberry Pis have no clean-shutdown-on-power-loss capability by
default, so an ungraceful power cycle is a normal, expected event over the
life of a deployment like this one -- not an edge case.

## Observed frequency (this deployment, 2026-08-07)

Two confirmed occurrences in one evening, both tied to abrupt process
termination:

1. **~13:41 PDT** -- after a cluster of ~5-6 manual `systemctl restart`
   calls within a ~4 minute window (routine verification testing, not
   normal operation). Crash loop went unnoticed/unfixed for several hours
   until it surfaced as a user-visible symptom (~19:00).
2. **~19:48 PDT** -- immediately after a single hard power-cycle
   (physical unplug/replug) of the Pi. Recurred in a cache directory that
   had only existed for ~45 minutes at that point (rebuilt clean after
   fixing occurrence #1) -- so this is not something that requires hours
   of runtime or many restarts to trigger. One abrupt termination was
   sufficient.

Sample size is small (2 events), but the mechanism is well understood and
both triggers (rapid restart, hard power loss) are things that will
recur over this device's normal lifetime -- this should be assumed to
happen again, not treated as a one-off.

## The fix

`services/ei-runner-cache-cleanup.sh` scans for zero-byte `.json` files
under the monitoring cache and deletes them. It's wired in two ways:

1. **On every `ei-runner.service` start** (`ExecStartPre=`, no age
   filter) -- since nothing is running yet at that point, any zero-byte
   file found is unambiguously stale from a previous crashed run, so it's
   safe to remove immediately. This makes a corrupted cache self-healing
   on restart instead of crash-looping forever.
2. **Every 15 minutes while running**
   (`ei-runner-cache-cleanup.timer` + `.service`, age filter: only files
   older than 5 minutes) -- catches corruption that happens mid-session
   without waiting for the next restart. The age filter exists because a
   genuinely brand-new file might just be mid-write, not corrupted --
   only files old enough that a normal write would have finished are
   treated as stale.

Both the static `services/ei-runner.service` template and the dashboard's
live `_build_ei_runner_unit()` generator include the startup cleanup step.

## What would actually fix this upstream

Ideally, `edge-impulse-linux-runner` itself would either write monitoring
cache files atomically (write to a temp file, `rename()` into place) or
validate/skip malformed files on startup instead of crashing on the first
bad one. Worth reporting to Edge Impulse if this deployment keeps hitting
it -- this cleanup script is a workaround, not a real fix.
