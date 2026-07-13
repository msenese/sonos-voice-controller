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
  breathing = idle/listening, amber breathing = Sonos muted or at ~0 volume,
  green flash = command recognized, red = disconnected from the EI runner,
  amber left-right chase = starting up / reconnecting) and toggles Sonos mute
  via the GPIO17 button. Polls Home Assistant every 5s for the Sonos mute/
  volume state independent of the button.
- **`sonos-dashboard.service`** — the Flask web dashboard (below), on port
  8080.
- **`audio-buffer.service`** — **deployed but deliberately disabled.** See
  "Trigger captures" below.

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

   `audio-buffer.service` also deploys alongside these but should be left
   disabled — see "Trigger captures" below.

4. Restart after future code changes:

   ```bash
   ssh msenese@192.168.50.99 "sudo systemctl restart ei-runner.service sonos-controller.service sonos-dashboard.service"
   ```

5. `sonos-controller.service` runs as root (needs SPI/GPIO access); the
   other services run as `msenese`. A few dashboard actions need passwordless
   `sudo` for that user — see `/etc/sudoers.d/sonos-dashboard` on the Pi
   (restart `ei-runner`/`sonos-controller`, stop/start `ei-runner` around
   recordings).

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
  Pi.
- **Trigger captures — built but currently inactive.** The intent: save the
  3s of audio around every detection so you can review it, confirm it, or
  send false positives to Edge Impulse as `unknown` training data. Blocked
  because `edge-impulse-linux-runner` captures audio via `sox` grabbing its
  hardware device directly and exclusively — it never goes through ALSA's
  sharing layer, so `audio-buffer.service` and `ei-runner.service` can't both
  hold the mic today. A fix is validated (`ei-runner` reading from an
  `snd-aloop` loopback device that `audio-buffer.py` forwards real audio
  into) but the duplex-forwarding implementation and soak testing aren't
  done — don't enable `audio-buffer.service` until that's finished.

## Known assumptions to double check

- The EI runner's websocket message shape is assumed to be
  `{"type": "classification", "result": {"classification": {label: score, ...}}}`,
  matching the code already deployed on the Pi.
- The Edge Impulse ingestion upload, retrain/build/download/activate flow,
  and sample-splitting workflow have all been exercised against the live
  project from this repo and are working as of this writing.
- `snd-aloop` may still be loaded on the Pi from loopback testing. It isn't
  persisted (no `/etc/modules-load.d/` entry), so a reboot clears it. This
  is safe either way now that `ei-runner` no longer relies on device
  auto-selection.
