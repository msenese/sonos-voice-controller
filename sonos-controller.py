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

time.sleep(15)
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


def set_leds(r, g, b):
    start = [0x00, 0x00, 0x00, 0x00]
    leds = [0xFF, b, g, r] * 3
    end = [0xFF, 0xFF, 0xFF, 0xFF]
    spi.xfer2(start + leds + end)


async def breathe():
    global led_override
    step = 0
    while True:
        if not led_override:
            brightness = max(3, min(25, int((math.sin(step) + 1) / 2 * 45)))
            set_leds(0, 0, brightness)
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


def trigger_ha(action):
    headers = {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    if action == "sonos pause":
        requests.post(f"{cfg.HA_URL}/api/services/media_player/media_pause",
            headers=headers,
            json={"entity_id": cfg.SONOS_ENTITY})
        print(f"[HA] Paused Sonos")
    elif action == "sonos play":
        requests.post(f"{cfg.HA_URL}/api/services/media_player/media_play",
            headers=headers,
            json={"entity_id": cfg.SONOS_ENTITY})
        print(f"[HA] Played Sonos")


async def watch_button():
    global is_muted
    last_state = GPIO.input(BUTTON_PIN)
    while True:
        try:
            current_state = GPIO.input(BUTTON_PIN)
            if last_state == GPIO.HIGH and current_state == GPIO.LOW:
                print("[BTN] Button pressed - toggling mute")
                headers = {
                    "Authorization": f"Bearer {cfg.HA_TOKEN}",
                    "Content-Type": "application/json"
                }
                response = requests.get(f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=headers)
                state = response.json()
                is_muted_current = state.get("attributes", {}).get("is_volume_muted", False)
                requests.post(f"{cfg.HA_URL}/api/services/media_player/volume_mute",
                    headers=headers,
                    json={"entity_id": cfg.SONOS_ENTITY, "is_volume_muted": not is_muted_current})
                is_muted = not is_muted_current
                write_state()
                await flash_green()
            last_state = current_state
        except Exception as e:
            print(f"[BTN] Error: {e}")
        await asyncio.sleep(0.05)


async def listen():
    global last_trigger_time, consecutive_count, latest_scores, connection_status
    while True:
        try:
            print("[EI] Attempting to connect to Edge Impulse runner...")
            connection_status = "connecting"
            set_leds(30, 10, 0)
            async with websockets.connect(cfg.EI_WS_URL) as ws:
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
                        if label in ["sonos pause", "sonos play"]:
                            threshold = cfg.SONOS_PLAY_THRESHOLD if label == "sonos play" else cfg.THRESHOLD
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
                                        await flash_green()
                            else:
                                consecutive_count[label] = 0
                    write_state()
        except Exception as e:
            print(f"[EI] Connection failed: {e}. Retrying in 5 seconds...")
            connection_status = "disconnected"
            write_state()
            set_leds(30, 0, 0)
            await asyncio.sleep(5)


async def main():
    await asyncio.gather(breathe(), listen(), watch_button())

asyncio.run(main())
