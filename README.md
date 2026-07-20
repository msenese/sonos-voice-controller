# Sonos Voice Controller

Voice-controlled Sonos playback using an Edge Impulse keyword-spotting model
running on a Raspberry Pi Zero W2 with a Seeed ReSpeaker 2-Mic HAT. Says
"sonos pause" or "sonos play" and it calls Home Assistant to control a Sonos
speaker. Includes a web dashboard for tuning thresholds, watching live
inference scores, recording new training samples, and retraining/redeploying
the model.

## Hardware

- Raspberry Pi Zero W2, currently at `192.168.50.99`
- Seeed ReSpeaker 2-Mic HAT (wm8960 codec, audio device `hw:1,0`)
- APA102 LEDs (SPI, `/dev/spidev0.0`) and a GPIO17 push button on the HAT
- Home Assistant instance (`192.168.50.212`) controlling a `media_player`
  entity (e.g. a Sonos speaker)

## How it works

Four systemd services run on boot:

- **`ei-runner.service`** — runs `edge-impulse-linux-runner` against the
  trained model (`sonos-model.eim`), classifying microphone audio into four
  labels: `noise`, `sonos pause`, `sonos play`, `unknown`. It exposes a local
  websocket at `ws://localhost:4912` streaming classification results.
  Pinned to `--microphone hw:1,0` explicitly — see the note below on why
  that matters.
- **`sonos-controller.service`** — connects to that websocket, and when a
  command label crosses its threshold for `CONSECUTIVE_REQUIRED` messages in
  a row (and the cooldown has elapsed), calls the Home Assistant REST API to
  pause/play the Sonos entity. It also drives the APA102 LEDs (aquamarine
  breathing = idle/listening, purple breathing = Sonos muted or at ~0 volume,
  green flash = command recognized, red = disconnected from the EI runner,
  amber left-right chase = starting up / reconnecting -- purple and amber
  used to both be amber and were easy to mix up at a glance) and toggles
  Sonos mute
  via the GPIO17 button. Polls Home Assistant every 5s for the Sonos mute/
  volume state independent of the button.
- **`sonos-dashboard.service`** — the Flask web dashboard (below), on port
  8080.
- **`audio-buffer.service`** — only runs when Audio Capture Mode is On (see
  "Audio Capture Mode" below). Owns the real microphone and forwards audio
  into an `snd-aloop` loopback device in real time, so `ei-runner` can keep
  detecting commands from the loopback while `audio-buffer.py` saves short
  recordings of each detection for review.

The controller writes its live state (current scores, connection status,
detection history, mute state) to `/tmp/sonos_controller_state.json` on every
classification message. The dashboard reads that file and polls it over
plain HTTP (there is no dashboard-side websocket) rather than opening a
second websocket connection to the EI runner.

Config values (`THRESHOLD`, `SONOS_PLAY_THRESHOLD`, `COOLDOWN`,
`CONSECUTIVE_REQUIRED`, `HA_URL`, etc.) are re-read from `config.py` on the
fly — the controller checks the file's mtime on every message, so changes
made from the dashboard take effect within a moment, no restart needed.

### Why `ei-runner.service` pins `--microphone hw:1,0`

Omitting `--microphone` lets the runner auto-select a capture device, which
works fine as long as there are only the two real ALSA capture devices
(`hw:0,0`, `hw:1,0`). Loading the `snd-aloop` kernel module (used for
loopback experiments, see below) adds a third device, and auto-select
silently started picking that one instead of the real mic — producing zero
audio and taking the whole detection pipeline down with no obvious error.
Pinning the device explicitly makes this deterministic regardless of what
else is loaded on the system. Don't remove this flag.

### Audio Capture Mode (Off / On)

The dashboard's "Audio Capture Mode" card switches which microphone
`ei-runner` reads from, by stopping `ei-runner`, starting or stopping
`audio-buffer.service`, rewriting `/etc/systemd/system/ei-runner.service`
with the right `--microphone` flag, and restarting both `ei-runner` and
`sonos-controller`. **This means the `ei-runner.service` unit file on the
Pi is a moving target** — the copy in `services/ei-runner.service` in this
repo is only the initial/Off-mode version installed on first setup; after
the first toggle switch, the live file on the Pi may differ (see
`dashboard/app.py`'s `_build_ei_runner_unit()` for the two variants it
writes).

- **Off** (`hw:1,0`, the real mic): `ei-runner` reads the microphone
  directly. Fastest, most tested, no recordings saved.
- **On** (`hw:2,0`, an `snd-aloop` loopback): `audio-buffer.py` owns the
  real mic and forwards audio into the loopback in real time; `ei-runner`
  detects commands from that loopback exactly as if it were the real mic,
  while `audio-buffer.py` also saves a short recording of each detection
  to `/home/msenese/trigger-captures/` for review in the dashboard's
  "Recordings to Review" card. Costs roughly +70MB of RAM at steady state
  on top of Off mode (measured on this Pi Zero W2's 425MB total).

If a switch to On fails to reconnect, the dashboard automatically switches
back to Off — you shouldn't ever get stuck with a dead detection pipeline
from using this toggle, but if you do, `POST /api/audio-mode` with
`{"mode": "classic"}` forces it back manually.

`snd-aloop` needs to be loaded before On mode will work:

```bash
ssh msenese@192.168.50.99 "sudo modprobe snd-aloop"
```

It isn't currently persisted across reboots (no `/etc/modules-load.d/`
entry) — add one if you want On mode to survive a reboot without a manual
`modprobe`.

## Setup

1. Copy `config.example.py` to `config.py` and fill in your real values:

   ```bash
   cp config.example.py config.py
   ```

   ```python
   HA_URL = "http://192.168.50.212:8123"
   HA_TOKEN = "your-token-here"        # Long-lived access token from HA
   EI_WS_URL = "ws://localhost:4912"
   SONOS_ENTITY = "media_player.office_1"
   THRESHOLD = 0.92
   SONOS_PLAY_THRESHOLD = 0.85
   COOLDOWN = 3.0
   CONSECUTIVE_REQUIRED = 2
   EI_API_KEY = "your-edge-impulse-api-key-here"        # Ingestion+deployment role; training upload, build, download
   EI_PROJECT_ID = "your-project-id-here"                # Visible in the Studio URL
   EI_ADMIN_API_KEY = "your-edge-impulse-admin-api-key-here"  # Admin role; only used to trigger retrain
   ```

   `config.py` is gitignored — never commit it. On the Pi it should be
   `chmod 600` (owner read/write only) since it holds live credentials.

2. Deploy to the Pi:

   ```bash
   scp config.py msenese@192.168.50.99:/home/msenese/
   scp sonos-controller.py msenese@192.168.50.99:/home/msenese/
   scp -r dashboard/ msenese@192.168.50.99:/home/msenese/
   ```

3. Install the systemd units (first time only):

   ```bash
   scp services/*.service msenese@192.168.50.99:/tmp/
   ssh msenese@192.168.50.99 "sudo mv /tmp/ei-runner.service /tmp/sonos-controller.service /tmp/sonos-dashboard.service /etc/systemd/system/ \
     && sudo systemctl daemon-reload \
     && sudo systemctl enable --now ei-runner.service sonos-controller.service sonos-dashboard.service"
   ```

   `audio-buffer.service` also deploys alongside these but stays inactive
   until you switch Audio Capture Mode On from the dashboard — don't
   `enable --now` it here.

4. Install the sudoers rules the dashboard needs (restarting services and
   switching Audio Capture Mode):

   ```bash
   scp services/sonos-dashboard.sudoers msenese@192.168.50.99:/tmp/
   ssh msenese@192.168.50.99 "sudo mv /tmp/sonos-dashboard.sudoers /etc/sudoers.d/sonos-dashboard \
     && sudo chmod 440 /etc/sudoers.d/sonos-dashboard \
     && sudo visudo -c"
   ```

   Edit the `msenese` username in that file first if the dashboard runs as
   a different user on your box. `sonos-controller.service` runs as root
   (needs SPI/GPIO access); the other services run as this user.

5. Restart after future code changes:

   ```bash
   ssh msenese@192.168.50.99 "sudo systemctl restart ei-runner.service sonos-controller.service sonos-dashboard.service"
   ```

## Dashboard

Runs automatically via `sonos-dashboard.service` at `http://192.168.50.99:8080`.
It shows:

- Live per-label inference scores, last detected command, detection history
- Mic capture level slider (`amixer -c 1 sset Capture`)
- Threshold/cooldown/consecutive-required sliders, written straight into
  `config.py`
- System status (uptime, CPU temp, memory)
- **Training mode**: record a clip (briefly pauses `ei-runner` to borrow the
  mic, then resumes it), upload to Edge Impulse's ingestion API, or review a
  longer recording as a waveform and split it into individually-labeled
  samples (mirrors Edge Impulse Studio's own split-sample tool)
- **Retrain & deploy**: trigger a retrain job, review accuracy and the
  confusion matrix, build and download a new model for the Pi, and activate
  it (backs up the outgoing model, swaps it in, restarts `ei-runner`). A
  successful activation auto-commits and pushes the new model to GitHub via
  a repo-scoped deploy key from a separate clone at `~/git-archive/` on the
  Pi. The card always shows which model is currently live (activation date
  and accuracy, from a `sonos-model.meta.json` sidecar file written on every
  activate/rollback) so this doesn't get lost track of over time.
- **Rollback to Previous Model** (only shown when a previous model exists):
  swaps `sonos-model.eim` and `sonos-model-previous.eim` and restarts
  `ei-runner`. This is a true swap, not an overwrite -- the outgoing live
  model becomes the new "previous", so clicking it twice undoes itself.
  Uses atomic renames rather than writing into the live file directly,
  since `ei-runner` may have it open/executing and an in-place write fails
  with `ETXTBSY` ("Text file busy") -- the same reason `activate` uses
  `PENDING_MODEL_PATH.replace(LIVE_MODEL_PATH)` instead of overwriting.
- **Audio Capture Mode** (Off/On): switches whether `ei-runner` reads the
  real mic directly or via the `audio-buffer.py` loopback forwarder — see
  "Audio Capture Mode" above for the full explanation and tradeoffs.
- **Recordings to Review** (only shown when Capture Mode is On): the ~3s
  clip saved around each detection. "Correct" uploads it to Edge Impulse
  under the label the detector assigned, reinforcing that class; the
  relabel dropdown lets you upload it under the *actual* correct label when
  the detector got it wrong (ambient noise, partial trigger, wrong wake
  word); "Discard" deletes it without uploading anything, for clips that
  aren't usable for training.
- **Sonos transport controls** in the header: play/pause, mute, and a
  volume slider that call Home Assistant's `media_player` services
  directly, independent of voice control.
- **Auto-Resume Playback** toggle in the header: while On, a background
  loop polls Home Assistant every ~1s and resumes playback the moment it
  sees the Sonos entity paused — including false-trigger pauses. Meant for
  actively gathering Recordings to Review without manually hitting play
  after every false trigger. Off by default; turn it off again once
  you're done testing rather than leaving it running unattended, since it
  will resume playback after *any* pause, including ones you meant.

## Known assumptions to double check

- The EI runner's websocket message shape is assumed to be
  `{"type": "classification", "result": {"classification": {label: score, ...}}}`,
  matching the code already deployed on the Pi.
- The Edge Impulse ingestion upload, retrain/build/download/activate flow,
  the sample-splitting workflow, Audio Capture Mode's duplex forwarding and
  toggle-with-rollback, and the Recordings to Review upload/relabel flow
  have all been exercised against the live project and Pi and are working
  as of this writing.
- `snd-aloop` isn't persisted across reboots (no `/etc/modules-load.d/`
  entry) — see "Audio Capture Mode" above.
