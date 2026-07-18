#!/usr/bin/env python3
"""Unattended background-noise sample collector.

Records short clips at a fixed interval and uploads each one to Edge Impulse
under the "noise" label, to build out negative-class training data without
needing to manually record and upload one at a time.

Usage: python3 collect-noise.py [duration_minutes]
"""
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import requests

import config as cfg

DEVICE = "plughw:CARD=wm8960soundcard,DEV=0"
SAMPLE_RATE = 16000
CHANNELS = 1
CLIP_SECONDS = 1.3
# Every fresh arecord invocation has a brief pop/click as the codec's ADC
# stabilizes right after the device opens -- capture a bit extra up front and
# discard it, rather than baking that pop into every single clip as a
# spurious signature the model could latch onto instead of real noise.
PRE_ROLL_SECONDS = 0.1
CAPTURE_SECONDS = CLIP_SECONDS + PRE_ROLL_SECONDS
CAPTURE_BYTES = int(SAMPLE_RATE * CAPTURE_SECONDS) * 2  # int16 = 2 bytes/sample
PRE_ROLL_BYTES = int(SAMPLE_RATE * PRE_ROLL_SECONDS) * 2
INTERVAL_SECONDS = 20
DEFAULT_DURATION_MINUTES = 20
LABEL = "noise"
OUTPUT_DIR = Path("/home/msenese/noise-samples")


def record_clip(path):
    proc = subprocess.Popen(
        [
            "arecord", "-D", DEVICE,
            "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
            "-t", "raw", "-q",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        raw = proc.stdout.read(CAPTURE_BYTES)
    finally:
        proc.kill()
        proc.wait()

    if len(raw) < CAPTURE_BYTES:
        stderr = proc.stderr.read().decode(errors="replace")
        return False, f"only got {len(raw)} of {CAPTURE_BYTES} bytes: {stderr[:200]}"

    trimmed = raw[PRE_ROLL_BYTES:]

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(trimmed)
    return True, None


def upload_to_ei(path):
    with path.open("rb") as f:
        return requests.post(
            "https://ingestion.edgeimpulse.com/api/training/files",
            headers={
                "x-api-key": cfg.EI_API_KEY,
                "x-label": LABEL,
            },
            files={"data": (path.name, f, "audio/wav")},
            timeout=30,
        )


def stop_ei_runner():
    print("Stopping ei-runner.service to free up the microphone...")
    result = subprocess.run(
        ["sudo", "systemctl", "stop", "ei-runner.service"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"Failed to stop ei-runner.service: {result.stderr.strip()}")
        sys.exit(1)


def start_ei_runner():
    print("Restarting ei-runner.service...")
    result = subprocess.run(
        ["sudo", "systemctl", "start", "ei-runner.service"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"Failed to restart ei-runner.service: {result.stderr.strip()}")
        return
    print("ei-runner.service restarted.")

    # sonos-controller's websocket client doesn't reliably reconnect on its own
    # when ei-runner restarts underneath it -- restart it too so live scores
    # don't silently go stale while connection_status still reads "connected".
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "sonos-controller.service"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"Failed to restart sonos-controller.service: {result.stderr.strip()}")
    else:
        print("sonos-controller.service restarted.")


def main():
    duration_minutes = DEFAULT_DURATION_MINUTES
    if len(sys.argv) > 1:
        try:
            duration_minutes = float(sys.argv[1])
        except ValueError:
            print(f"Invalid duration {sys.argv[1]!r}, using default of {DEFAULT_DURATION_MINUTES} minutes.")

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(
        f"Collecting noise samples for {duration_minutes:.0f} minutes "
        f"({CLIP_SECONDS}s clips every {INTERVAL_SECONDS}s, ~"
        f"{int(duration_minutes * 60 / (CLIP_SECONDS + INTERVAL_SECONDS))} samples expected)."
    )
    print(f"Saving to {OUTPUT_DIR}, uploading to Edge Impulse under label {LABEL!r}.")

    recorded = 0
    uploaded = 0
    failed = 0

    stop_ei_runner()
    try:
        end_time = time.time() + duration_minutes * 60
        while time.time() < end_time:
            recorded += 1
            timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            filename = f"{timestamp}-noise.wav"
            path = OUTPUT_DIR / filename

            ok, err = record_clip(path)
            if not ok:
                print(f"[{recorded}] recording failed: {err}")
                failed += 1
                time.sleep(INTERVAL_SECONDS)
                continue

            try:
                response = upload_to_ei(path)
            except requests.RequestException as e:
                print(f"[{recorded}] {filename}: recorded, upload error: {e}")
                failed += 1
                time.sleep(INTERVAL_SECONDS)
                continue

            if response.status_code >= 300:
                print(f"[{recorded}] {filename}: recorded, upload failed: {response.status_code} {response.text[:200]}")
                failed += 1
            else:
                uploaded += 1
                remaining_min = max(0, end_time - time.time()) / 60
                print(f"[{recorded}] {filename}: uploaded ({uploaded} uploaded, {failed} failed, ~{remaining_min:.1f} min left)")

            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        start_ei_runner()

    print(f"Done. {recorded} clips recorded, {uploaded} uploaded, {failed} failed.")


if __name__ == "__main__":
    main()
