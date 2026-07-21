import asyncio
import websockets
import json
import requests
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

last_trigger_time = 0
consecutive_count = {}
led_override = False

latest_scores = {}
detection_history = []
connection_status = "disconnected"
is_muted = None

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
    tmp_path = STATE_FILE + ".tmp"
    try:
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


def toggle_mute():
    # Shared by the physical button and the "sonos mute" voice trigger, so
    # both paths produce identical behavior: read the current HA mute
    # state, flip it, and let breathe() pick up the new `is_muted` value
    # for LED feedback (violet breathing) exactly as it already does for
    # the button.
    global is_muted
    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.get(f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers, timeout=HA_REQUEST_TIMEOUT)
    state = response.json()
    is_muted_current = state.get("attributes", {}).get("is_volume_muted", False)
    requests.post(f"{cfg.HA_URL}/api/services/media_player/volume_mute",
        headers=headers,
        json={"entity_id": cfg.SONOS_ENTITY, "is_volume_muted": not is_muted_current},
        timeout=HA_REQUEST_TIMEOUT)
    is_muted = not is_muted_current
    write_state()


def trigger_ha(action):
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
        response = requests.post(
            f"{cfg.HA_URL}/api/services/media_player/{endpoint}",
            headers=headers,
            json={"entity_id": cfg.SONOS_ENTITY},
            timeout=HA_REQUEST_TIMEOUT,
        )
        if response.status_code >= 300:
            print(f"[HA] {endpoint} failed: HTTP {response.status_code} {response.text[:200]}")
        else:
            print(f"[HA] {'Paused' if action == 'sonos pause' else 'Played'} Sonos")
    except Exception as e:
        print(f"[HA] {endpoint} error: {e}")


async def watch_button():
    last_state = GPIO.input(BUTTON_PIN)
    while True:
        try:
            current_state = GPIO.input(BUTTON_PIN)
            if last_state == GPIO.HIGH and current_state == GPIO.LOW:
                print("[BTN] Button pressed - toggling mute")
                toggle_mute()
                await flash_green()
            last_state = current_state
        except Exception as e:
            print(f"[BTN] Error: {e}")
        await asyncio.sleep(0.05)


async def poll_mute_state():
    global is_muted
    while True:
        try:
            headers = {
                "Authorization": f"Bearer {cfg.HA_TOKEN}",
                "Content-Type": "application/json"
            }
            response = requests.get(f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers, timeout=HA_REQUEST_TIMEOUT)
            state = response.json()
            attributes = state.get("attributes", {})
            volume_muted = attributes.get("is_volume_muted", False)
            volume_level = attributes.get("volume_level", 1.0)
            is_muted = bool(volume_muted) or volume_level <= 0.02
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
                                        trigger_ha(label)
                                        post_capture(label, score)
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
    await asyncio.gather(breathe(), listen(), watch_button(), poll_mute_state())

asyncio.run(main())
