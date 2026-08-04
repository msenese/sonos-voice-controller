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

### Why `ei-runner.service` pins `--microphone hw:$CARD,0`

Omitting `--microphone` lets the runner auto-select a capture device, which
works fine as long as there are only the two real ALSA capture devices.
Loading the `snd-aloop` kernel module (used for loopback experiments, see
below) adds a third device, and auto-select silently started picking that
one instead of the real mic — producing zero audio and taking the whole
detection pipeline down with no obvious error. Pinning the device explicitly
makes this deterministic regardless of what else is loaded on the system.
Don't remove this flag.

Two things confirmed live during a from-scratch rebuild, both already
reflected in `services/ei-runner.service` — worth knowing if you ever hand-edit it:

- **`edge-impulse-linux-runner` rejects named ALSA addressing outright.**
  `--microphone hw:wm8960soundcard,0` fails with *"cannot find microphones
  with that name"* — it only accepts numeric `hw:N,0` matching its own
  enumeration. Card *numbers* aren't stable across reboots (load order can
  shift them — confirmed both `hw:0,0`/`hw:1,0` swap depending on what's
  loaded), so the unit resolves the current number for the `wm8960soundcard`
  card id at each start via `grep -oP` against `/proc/asound/cards`, rather
  than hardcoding a number.
- Systemd logs `Ignoring unknown escape sequences` for that `grep -oP`
  pattern (the `\K`/`\s`/`\[` inside it) on every start. This is cosmetic —
  confirmed live that the command still resolves and runs correctly despite
  the warning — but it's noisy enough in the journal to be worth knowing
  it's not an actual failure.

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

Assumes a fresh Raspberry Pi OS install (flashed via Raspberry Pi Imager,
SSH enabled, on the network) with the ReSpeaker/WM8960 HAT physically
attached. Node.js is **not** preinstalled on a stock image — installed
below.

0. Enable SPI (needed for the APA102 LEDs on `/dev/spidev0.0`) — it's
   present but commented out in a fresh image's `config.txt`, so
   `sonos-controller.py` fails immediately with `FileNotFoundError` on
   `spi.open(0, 0)` until this is done:

   ```bash
   ssh msenese@192.168.50.99 "sudo sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' /boot/firmware/config.txt && sudo reboot"
   ```

   Confirm after reboot: `ssh msenese@192.168.50.99 "ls /dev/spidev0.0"`
   should exist.

1. Install the WM8960 audio driver, Node.js, and the Edge Impulse CLI.

   The HAT needs an out-of-tree DKMS kernel module — it's not in mainline
   and won't work by just loading a stock overlay. This repo vendors a
   known-working copy at [`driver/wm8960-soundcard-1.0/`](driver/wm8960-soundcard-1.0/)
   (originally from
   [`github.com/waveshare/WM8960-Audio-HAT`](https://github.com/waveshare/WM8960-Audio-HAT),
   pulled from `/usr/src/wm8960-soundcard-1.0` on a working Pi — keep this
   vendored copy even if upstream changes, since it's confirmed to build
   against the exact kernel this project runs on):

   ```bash
   scp -r driver/wm8960-soundcard-1.0 msenese@192.168.50.99:/tmp/
   ssh msenese@192.168.50.99 "cd /tmp/wm8960-soundcard-1.0 && sudo ./install.sh && sudo reboot"
   ```

   After it reboots, confirm the card is up (`dtoverlay=wm8960-soundcard`
   and `dtoverlay=i2s-mmap` should both already be in
   `/boot/firmware/config.txt` — `install.sh` adds them):

   ```bash
   ssh msenese@192.168.50.99 "aplay -l && arecord -l && sudo dkms status"
   ```

   You should see `card 1: wm8960soundcard` in both listings, and
   `wm8960-soundcard/1.0, ..., installed` from `dkms status`.

   Install Node.js (not on a stock image) and `sox` (the runner shells out
   to it for audio capture — without it, `ei-runner.service` starts and
   immediately fails with `Failed to run impulse Missing "sox" in PATH`):

   ```bash
   ssh msenese@192.168.50.99 "sudo apt-get install -y nodejs npm sox"
   ```

   Then install the Edge Impulse CLI (provides `edge-impulse-linux-runner`,
   used by `ei-runner.service`):

   ```bash
   ssh msenese@192.168.50.99 "sudo npm install -g edge-impulse-linux --unsafe-perm"
   ```

   `npm` installs the binary to `/usr/local/bin/edge-impulse-linux-runner`,
   but `services/ei-runner.service` calls `/usr/bin/edge-impulse-linux-runner`
   — symlink it, or `ei-runner.service` fails with `status=203/EXEC`:

   ```bash
   ssh msenese@192.168.50.99 "sudo ln -sf \$(which edge-impulse-linux-runner) /usr/bin/edge-impulse-linux-runner"
   ```

   Finally, install the Python dependencies (there's no `requirements.txt`
   yet — every service invokes plain `/usr/bin/python3`, no venv, so these
   need to go in system-wide; Debian Trixie blocks a plain `pip3 install`
   with an externally-managed-environment error, hence `--break-system-packages`,
   which is the documented escape hatch for exactly this single-purpose-device case):

   ```bash
   ssh msenese@192.168.50.99 "sudo pip3 install websockets requests RPi.GPIO spidev flask numpy onnxruntime sounddevice --break-system-packages"
   ```

2. Copy `config.example.py` to `config.py` and fill in your real values:

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

3. Deploy to the Pi — including the model file itself, which
   `ei-runner.service` won't start without:

   ```bash
   scp config.py msenese@192.168.50.99:/home/msenese/
   scp sonos-controller.py msenese@192.168.50.99:/home/msenese/
   scp -r dashboard/ msenese@192.168.50.99:/home/msenese/
   scp models/sonos-model-current.eim msenese@192.168.50.99:/home/msenese/sonos-model.eim
   ssh msenese@192.168.50.99 "chmod +x /home/msenese/sonos-model.eim && chmod 600 /home/msenese/config.py"
   ```

4. Install the systemd units (first time only):

   ```bash
   scp services/*.service msenese@192.168.50.99:/tmp/
   ssh msenese@192.168.50.99 "sudo mv /tmp/ei-runner.service /tmp/sonos-controller.service /tmp/sonos-dashboard.service /etc/systemd/system/ \
     && sudo systemctl daemon-reload \
     && sudo systemctl enable --now ei-runner.service sonos-controller.service sonos-dashboard.service"
   ```

   `audio-buffer.service` also deploys alongside these but stays inactive
   until you switch Audio Capture Mode On from the dashboard — don't
   `enable --now` it here.

5. Install the sudoers rules the dashboard needs (restarting services and
   switching Audio Capture Mode). **`sudo mv` alone leaves the file owned
   by your user, not root** — `visudo -c` will report `wrong owner (uid,
   gid) should be (0, 0)` and sudo will silently ignore the file until
   it's `chown`'d:

   ```bash
   scp services/sonos-dashboard.sudoers msenese@192.168.50.99:/tmp/
   ssh msenese@192.168.50.99 "sudo mv /tmp/sonos-dashboard.sudoers /etc/sudoers.d/sonos-dashboard \
     && sudo chown root:root /etc/sudoers.d/sonos-dashboard \
     && sudo chmod 440 /etc/sudoers.d/sonos-dashboard \
     && sudo visudo -c"
   ```

   Edit the `msenese` username in that file first if the dashboard runs as
   a different user on your box. `sonos-controller.service` runs as root
   (needs SPI/GPIO access); the other services run as this user.

6. Restart after future code changes:

   ```bash
   ssh msenese@192.168.50.99 "sudo systemctl restart ei-runner.service sonos-controller.service sonos-dashboard.service"
   ```

7. Set up the git-archive auto-push clone (optional, but the dashboard's
   Retrain & Deploy flow silently no-ops the archive step without it —
   see `archive_model_to_git()` in `dashboard/app.py`, which requires
   `~/git-archive/sonos-voice-controller` to already exist on the Pi as a
   clone with push access):

   ```bash
   # Generate a deploy key ON THE PI (so the private key never leaves it):
   ssh msenese@192.168.50.99 "ssh-keygen -t ed25519 -f ~/.ssh/sonos_deploy_key -N '' -C sonos-pi-archive"
   ssh msenese@192.168.50.99 "cat ~/.ssh/sonos_deploy_key.pub"
   ```

   Add that public key as a **deploy key with write access** on the
   `msenese/sonos-voice-controller` GitHub repo (Settings → Deploy keys →
   Add deploy key). Then:

   ```bash
   ssh msenese@192.168.50.99 "cat >> ~/.ssh/config <<'EOF'
   Host github.com-sonos-archive
     HostName github.com
     User git
     IdentityFile ~/.ssh/sonos_deploy_key
     IdentitiesOnly yes
   EOF
   chmod 600 ~/.ssh/config
   # A fresh Pi has never talked to GitHub over SSH before -- without this,
   # the clone below fails with 'Host key verification failed'.
   ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
   mkdir -p ~/git-archive
   git clone git@github.com-sonos-archive:msenese/sonos-voice-controller.git ~/git-archive/sonos-voice-controller"
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

## Known-good milestones

The dashboard's "Rollback to Previous Model" only remembers one prior
generation — fine for undoing a bad retrain, but it silently stops
reaching any given model as soon as two more get activated after it. For
a model worth being able to restore indefinitely (e.g. one that's been
demoed or otherwise validated), tag its commit instead:

```bash
git tag -a milestone-name <commit> -m "why this one matters"
git push origin milestone-name
```

Find the right commit by matching the live model's activation metadata
(`GET /api/model/status` on the dashboard) against the auto-archive
commit with the same date/accuracy, and confirm it's the exact same
artifact — not just close — before tagging:

```bash
git show <commit>:models/sonos-model-current.eim | shasum -a 256
ssh msenese@192.168.50.99 "sha256sum /home/msenese/sonos-model.eim"
```

**`milestone-v1`** — activated 2026-07-18 19:13 PDT, 97.7% validation
accuracy, demoed live to staff engineers. Verified byte-for-byte
identical to the model running on the Pi at tagging time (SHA256
`727c6830...`). To restore it: `git show milestone-v1:models/sonos-model-current.eim > sonos-model.eim`,
then deploy and activate that file the normal way (Setup step 2, or via
the dashboard's Build & Activate flow using this file directly).

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
