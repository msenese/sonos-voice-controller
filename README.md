# Sonos Voice Controller

Voice-controlled Sonos playback using an Edge Impulse keyword-spotting model
running on a Raspberry Pi Zero W2 with a Seeed ReSpeaker 2-Mic HAT. Says
"sonos pause" or "sonos play" and it calls Home Assistant to control a Sonos
speaker. Includes a small web dashboard for tuning thresholds, watching live
inference scores, and recording new training samples.

## Hardware

- Raspberry Pi Zero W2
- Seeed ReSpeaker 2-Mic HAT (wm8960 codec, audio device `hw:1,0`)
- APA102 LEDs (SPI, `/dev/spidev0.0`) and a GPIO17 push button on the HAT
- Home Assistant instance controlling a `media_player` entity (e.g. a Sonos
  speaker)

## How it works

Two systemd services run on boot:

- **`ei-runner.service`** — runs `edge-impulse-linux-runner` against the
  trained model (`sonos-model.eim`), classifying microphone audio into four
  labels: `noise`, `sonos pause`, `sonos play`, `unknown`. It exposes a local
  websocket at `ws://localhost:4912` streaming classification results.
- **`sonos-controller.service`** — connects to that websocket, and when a
  command label crosses its threshold for `CONSECUTIVE_REQUIRED` messages in
  a row (and the cooldown has elapsed), calls the Home Assistant REST API to
  pause/play the Sonos entity. It also drives the APA102 LEDs (blue breathing
  = idle/listening, green flash = command recognized, red = disconnected from
  the EI runner) and toggles Sonos mute via the GPIO17 button.

The controller writes its live state (current scores, connection status,
detection history, mute state) to `/tmp/sonos_controller_state.json` on every
classification message. The dashboard reads that file rather than opening a
second websocket connection to the EI runner.

Config values (`THRESHOLD`, `SONOS_PLAY_THRESHOLD`, `COOLDOWN`,
`CONSECUTIVE_REQUIRED`) are re-read from `config.py` on the fly — the
controller checks the file's mtime on every message, so changes made from the
dashboard take effect within a moment, no restart needed.

## Setup

1. Copy `config.example.py` to `config.py` and fill in your Home Assistant
   token:

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
   EI_API_KEY = "your-edge-impulse-api-key-here"   # only needed for training upload
   ```

   `config.py` is gitignored — never commit it.

2. Deploy to the Pi:

   ```bash
   scp config.py msenese@10.0.0.155:/home/msenese/
   scp sonos-controller.py msenese@10.0.0.155:/home/msenese/
   scp -r dashboard/ msenese@10.0.0.155:/home/msenese/
   ```

3. Install the systemd units (first time only):

   ```bash
   scp services/*.service msenese@10.0.0.155:/tmp/
   ssh msenese@10.0.0.155 "sudo mv /tmp/ei-runner.service /tmp/sonos-controller.service /etc/systemd/system/ \
     && sudo systemctl daemon-reload \
     && sudo systemctl enable --now ei-runner.service sonos-controller.service"
   ```

4. Restart after future code changes:

   ```bash
   ssh msenese@10.0.0.155 "sudo systemctl restart ei-runner.service sonos-controller.service"
   ```

## Dashboard

```bash
ssh msenese@10.0.0.155
cd dashboard
pip3 install flask requests   # first time only
python3 app.py
```

Open `http://10.0.0.155:8080`. It shows:

- Live per-label inference scores
- Last detected command + timestamp, and a detection history log
- Mic capture level slider (`amixer -c 1 sset Capture`)
- Threshold/cooldown/consecutive-required sliders, written straight into
  `config.py`
- System status (uptime, CPU temp, memory)
- Training mode: pick a label, record a short clip, upload it to Edge
  Impulse's ingestion API (requires `EI_API_KEY` in `config.py`)

Run it under a systemd unit or `tmux`/`screen` if you want it to survive
disconnecting your SSH session — it isn't installed as a service by default
since it's meant to be opened occasionally, not run as a critical daemon.

## Known assumptions to double check

- The EI runner's websocket message shape is assumed to be
  `{"type": "classification", "result": {"classification": {label: score, ...}}}`,
  matching the code already deployed on the Pi.
- The Edge Impulse ingestion upload (`POST https://ingestion.edgeimpulse.com/api/training/files`
  with `x-api-key` / `x-label` headers and the file as a `data` multipart
  field) is implemented per Edge Impulse's public ingestion API docs but
  hasn't been exercised against a live project from this repo — verify the
  first upload works before relying on it.
