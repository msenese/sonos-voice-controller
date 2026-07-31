import asyncio
import websockets
import json
import requests
import threading
import time
import spidev
import math
import RPi.GPIO as GPIO
import subprocess
import importlib
import os

import config as cfg

STATE_FILE = "/tmp/sonos_controller_state.json"
HISTORY_LIMIT = 50
BUTTON_PIN = 17

_state_write_lock = threading.Lock()

last_trigger_time = 0
consecutive_count = {}
led_override = False

latest_scores = {}
detection_history = []
connection_status = "disconnected"
is_muted = None
is_paused = None

_config_mtime = os.path.getmtime(cfg.__file__)

GPIO.setmode(GPIO.BCM)
GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1000000

def set_leds(r, g, b):
    start = [0x00, 0x00, 0x00, 0x00]
    leds = [0xFF, b, g, r] * 3
    end = [0xFF, 0xFF, 0xFF, 0xFF]
    spi.xfer2(start + leds + end)


def set_individual_leds(colors):
    start = [0x00, 0x00, 0x00, 0x00]
    leds = []
    for (r, g, b) in colors:
        leds += [0xFF, b, g, r]
    end = [0xFF, 0xFF, 0xFF, 0xFF]
    spi.xfer2(start + leds + end)


# Bounces 0-1-2-1 repeating, i.e. "1-2-3-2-1" in 1-indexed LED positions.
CHASE_SEQUENCE = [0, 1, 2, 1]
BOOT_COLOR = (30, 10, 0)


def chase_frame(index, color=BOOT_COLOR):
    colors = [(0, 0, 0)] * 3
    colors[CHASE_SEQUENCE[index % len(CHASE_SEQUENCE)]] = color
    set_individual_leds(colors)


for _ in range(100):
    chase_frame(_)
    time.sleep(0.15)
subprocess.run(['amixer', '-c', '1', 'sset', 'Capture', '50'], capture_output=True)


def reload_config_if_changed():
    global _config_mtime
    try:
        mtime = os.path.getmtime(cfg.__file__)
        if mtime != _config_mtime:
            importlib.reload(cfg)
            _config_mtime = mtime
            print("[CONFIG] Reloaded")
    except OSError:
        pass


def write_state():
    state = {
        "scores": latest_scores,
        "history": detection_history[-HISTORY_LIMIT:],
        "connection_status": connection_status,
        "muted": is_muted,
        "updated_at": time.time(),
    }
    # write_state() is called from several async tasks that now run on
    # separate threads (poll_player_state, watch_button, trigger_ha via
    # asyncio.to_thread) -- without a lock, two concurrent calls sharing the
    # same fixed tmp_path can race: one overwrites the other's tmp file
    # before it renames, so the second rename fails with ENOENT even
    # though nothing about the actual command failed.
    tmp_path = STATE_FILE + ".tmp"
    try:
        with _state_write_lock:
            with open(tmp_path, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, STATE_FILE)
    except OSError as e:
        print(f"[STATE] Failed to write state file: {e}")


async def breathe():
    global led_override
    step = 0
    while True:
        if not led_override:
            if is_muted:
                # Computed directly from the sine wave rather than scaling down
                # the (already clamped-to-3..25) non-muted `brightness` value --
                # shrinking that by 0.25 and flooring at 2 collapsed the usable
                # range to ~2 integer steps, so it looked like it snapped
                # between two fixed colors instead of breathing smoothly.
                muted_brightness = max(2, int(2 + (math.sin(step) + 1) / 2 * 6))
                # Red must never truncate to 0 here -- int(2 * 0.25) and
                # int(3 * 0.25) both round down to 0, which briefly turned the
                # dim end of the cycle pure blue with no violet tint at all.
                set_leds(max(1, int(muted_brightness * 0.25)), int(muted_brightness * 0.1), muted_brightness)
            elif is_paused:
                # Reuses the same wide 3-25 sine curve as the normal (playing)
                # state -- proven smooth already -- rather than a new range,
                # but halves it before output: driving both R and G to the
                # same level reads as much brighter than G alone at an
                # identical number (human vision weights green more heavily
                # than red), so the raw curve looked blown-out and barely
                # breathing at all until it was scaled down.
                brightness = max(3, min(25, int((math.sin(step) + 1) / 2 * 45)))
                yellow_level = max(1, int(brightness * 0.5))
                set_leds(yellow_level, yellow_level, 0)
            else:
                brightness = max(3, min(25, int((math.sin(step) + 1) / 2 * 45)))
                set_leds(0, brightness, int(brightness * 0.8))
            step += 0.04
        await asyncio.sleep(0.05)


async def flash_green():
    global led_override
    led_override = True
    for _ in range(2):
        set_leds(0, 20, 0)
        await asyncio.sleep(0.1)
        set_leds(0, 0, 0)
        await asyncio.sleep(0.1)
    led_override = False


async def chase(color=BOOT_COLOR):
    i = 0
    try:
        while True:
            chase_frame(i, color)
            i += 1
            await asyncio.sleep(0.15)
    except asyncio.CancelledError:
        pass


HA_REQUEST_TIMEOUT = 5
HA_MAX_RETRIES = 2
HA_RETRY_DELAY = 1


def _ha_request(method, url, **kwargs):
    # A brief HA hiccup (restart, network blip) used to either hang the whole
    # controller -- these calls were made synchronously from inside asyncio
    # coroutines, so a slow/stuck request froze breathe()'s LEDs, GPIO button
    # polling, and ei-runner message handling all at once, since everything
    # shares one event loop thread -- or silently drop the command with no
    # retry. Callers now run this via asyncio.to_thread so it can't block the
    # loop, and it retries transient connection/timeout errors itself so a
    # short blip doesn't just lose the command.
    kwargs.setdefault("timeout", HA_REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(HA_MAX_RETRIES + 1):
        try:
            return requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < HA_MAX_RETRIES:
                time.sleep(HA_RETRY_DELAY)
    raise last_exc


def post_capture(label, score):
    # audio-buffer.service is currently disabled (see services/audio-buffer.service
    # and the Trigger Captures feature notes) -- connection failures here are
    # expected until that's re-enabled, so stay silent rather than log noise
    # on every single detection.
    try:
        requests.post(
            "http://localhost:8081/capture",
            json={"label": label, "score": score},
            timeout=2,
        )
    except Exception:
        pass


def _set_muted(muted):
    # Sets an absolute mute state rather than toggling -- used where the
    # desired end state is known (e.g. "sonos play" auto-unmuting), where
    # toggling off a possibly-stale `is_muted` read could flip the wrong
    # way and mute instead of unmute.
    global is_muted
    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    _ha_request("POST", f"{cfg.HA_URL}/api/services/media_player/volume_mute",
        headers=headers,
        json={"entity_id": cfg.SONOS_ENTITY, "is_volume_muted": muted})
    is_muted = muted
    write_state()


def toggle_mute():
    # Shared by the physical button and the "sonos mute" voice trigger, so
    # both paths produce identical behavior: read the current HA mute
    # state, flip it, and let breathe() pick up the new `is_muted` value
    # for LED feedback (violet breathing) exactly as it already does for
    # the button.
    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    response = _ha_request("GET", f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers)
    state = response.json()
    is_muted_current = state.get("attributes", {}).get("is_volume_muted", False)
    _set_muted(not is_muted_current)


def trigger_ha(action):
    global is_paused

    if action == "sonos mute":
        try:
            toggle_mute()
            print(f"[HA] Toggled mute via voice (now {'muted' if is_muted else 'unmuted'})")
        except Exception as e:
            print(f"[HA] mute toggle error: {e}")
        return

    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    endpoint = "media_pause" if action == "sonos pause" else "media_play" if action == "sonos play" else None
    if endpoint is None:
        return
    try:
        response = _ha_request(
            "POST", f"{cfg.HA_URL}/api/services/media_player/{endpoint}",
            headers=headers,
            json={"entity_id": cfg.SONOS_ENTITY},
        )
        if response.status_code >= 300:
            print(f"[HA] {endpoint} failed: HTTP {response.status_code} {response.text[:200]}")
        else:
            print(f"[HA] {'Paused' if action == 'sonos pause' else 'Played'} Sonos")
            # Update immediately rather than waiting up to 5s for
            # poll_player_state()'s next cycle, so the LED color change
            # (breathe()'s is_paused check) tracks the voice command instead
            # of visibly lagging behind it.
            is_paused = action == "sonos pause"
            write_state()

            # "sonos play" while muted+paused should bring back both --
            # otherwise playback resumes silently and it looks like the
            # command did nothing.
            if action == "sonos play" and is_muted:
                try:
                    _set_muted(False)
                    print("[HA] Also unmuted (was muted) as part of sonos play")
                except Exception as e:
                    print(f"[HA] auto-unmute error: {e}")

            # "sonos play" should always be audible -- if the volume was
            # manually left at 0, unmuting/unpausing alone still plays
            # silently. Only nudge it up from exactly 0; leave any other
            # level untouched.
            if action == "sonos play":
                try:
                    vol_response = _ha_request(
                        "GET", f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers)
                    volume_level = vol_response.json().get("attributes", {}).get("volume_level", 1.0)
                    if volume_level is not None and volume_level <= 0.0:
                        _ha_request(
                            "POST", f"{cfg.HA_URL}/api/services/media_player/volume_set",
                            headers=headers,
                            json={"entity_id": cfg.SONOS_ENTITY, "volume_level": 0.1})
                        print("[HA] Volume was 0 -- raised to 10% as part of sonos play")
                except Exception as e:
                    print(f"[HA] auto-volume error: {e}")
    except Exception as e:
        print(f"[HA] {endpoint} error: {e}")


async def watch_button():
    last_state = GPIO.input(BUTTON_PIN)
    while True:
        try:
            current_state = GPIO.input(BUTTON_PIN)
            if last_state == GPIO.HIGH and current_state == GPIO.LOW:
                print("[BTN] Button pressed - toggling mute")
                await asyncio.to_thread(toggle_mute)
                await flash_green()
            last_state = current_state
        except Exception as e:
            print(f"[BTN] Error: {e}")
        await asyncio.sleep(0.05)


def _fetch_player_state():
    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    response = _ha_request("GET", f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers)
    state = response.json()
    attributes = state.get("attributes", {})
    volume_muted = attributes.get("is_volume_muted", False)
    volume_level = attributes.get("volume_level", 1.0)
    muted = bool(volume_muted) or volume_level <= 0.02
    paused = state.get("state") == "paused"
    return muted, paused


async def poll_player_state():
    global is_muted, is_paused
    while True:
        try:
            is_muted, is_paused = await asyncio.to_thread(_fetch_player_state)
            write_state()
        except Exception as e:
            print(f"[HA] Poll error: {e}")
        await asyncio.sleep(5)


async def listen():
    global last_trigger_time, consecutive_count, latest_scores, connection_status, led_override
    while True:
        chase_task = None
        try:
            print("[EI] Attempting to connect to Edge Impulse runner...")
            connection_status = "connecting"
            led_override = True
            chase_task = asyncio.create_task(chase())
            async with websockets.connect(cfg.EI_WS_URL) as ws:
                chase_task.cancel()
                led_override = False
                print("[EI] Connected to Edge Impulse runner")
                connection_status = "connected"
                async for message in ws:
                    reload_config_if_changed()
                    data = json.loads(message)
                    if data.get("type") != "classification":
                        continue
                    result = data["result"]["classification"]
                    latest_scores = result
                    now = time.time()
                    for label, score in result.items():
                        if label in ["sonos pause", "sonos play", "sonos mute"]:
                            enabled = (
                                cfg.SONOS_PLAY_ENABLED if label == "sonos play"
                                else cfg.SONOS_MUTE_ENABLED if label == "sonos mute"
                                else cfg.SONOS_PAUSE_ENABLED
                            )
                            if not enabled:
                                # Gate before any threshold/consecutive/cooldown
                                # logic runs at all -- a disabled label should
                                # never trigger regardless of score, not just
                                # be made harder to trigger.
                                consecutive_count[label] = 0
                                continue
                            threshold = (
                                cfg.SONOS_PLAY_THRESHOLD if label == "sonos play"
                                else cfg.SONOS_MUTE_THRESHOLD if label == "sonos mute"
                                else cfg.THRESHOLD
                            )
                            if score >= threshold:
                                consecutive_count[label] = consecutive_count.get(label, 0) + 1
                                if consecutive_count[label] >= cfg.CONSECUTIVE_REQUIRED:
                                    if now - last_trigger_time >= cfg.COOLDOWN:
                                        last_trigger_time = now
                                        consecutive_count[label] = 0
                                        print(f"[DETECT] {label} ({score:.2f})")
                                        detection_history.append({
                                            "label": label,
                                            "score": score,
                                            "timestamp": now,
                                        })
                                        await asyncio.to_thread(trigger_ha, label)
                                        await asyncio.to_thread(post_capture, label, score)
                                        await flash_green()
                            else:
                                consecutive_count[label] = 0
                    write_state()
        except Exception as e:
            if chase_task is not None:
                chase_task.cancel()
            led_override = False
            print(f"[EI] Connection failed: {e}. Retrying in 5 seconds...")
            connection_status = "disconnected"
            write_state()
            set_leds(30, 0, 0)
            await asyncio.sleep(5)


async def main():
    await asyncio.gather(breathe(), listen(), watch_button(), poll_player_state())

asyncio.run(main())
