import queue
import re
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from flask import Flask, abort, jsonify, request

SAMPLE_RATE = 16000
CHANNELS = 1
BUFFER_SECONDS = 3
BUFFER_SAMPLES = SAMPLE_RATE * BUFFER_SECONDS
BLOCK_SIZE = 1600  # 100ms per callback
BLOCK_BYTES = BLOCK_SIZE * 2  # int16

# edge-impulse-linux-runner captures audio via sox grabbing its configured
# hardware device directly and exclusively -- it never goes through ALSA's
# dsnoop/plug sharing layer, regardless of device name. So this service
# owns the real mic exclusively and forwards what it hears into an
# snd-aloop loopback device in real time; ei-runner is pointed at the
# loopback's capture side instead of the real hardware (see
# services/ei-runner.service and the Classic/Buffer toggle in the
# dashboard). Validated pairing on this Pi: playing into the loopback's
# device-1 side comes out for capture on its device-0 side (not the
# reverse -- ei-runner's own microphone enumeration only lists the
# loopback's device 0, not device 1).
#
# Input uses an `arecord` subprocess, not sounddevice/PortAudio directly:
# PortAudio's ALSA enumeration reports the wm8960 hardware as "0 in, 2 out"
# on this Pi regardless of device string, even though arecord opens and
# reads it fine -- a PortAudio-specific enumeration limitation with this
# card, not an ALSA one. The loopback devices *do* enumerate correctly
# under PortAudio, so the output/forwarding side uses sounddevice normally.
#
# Both device references are named rather than numbered -- ALSA card
# *numbers* are assigned by load order and can shift across reboots (this
# Pi has already swapped which of wm8960/Loopback lands on card 1 vs. 2).
# Unlike edge-impulse-linux-runner (which only accepts numeric hw:N,0 from
# its own enumeration), arecord and PortAudio both resolve named ALSA
# addressing/descriptive names correctly, so no runtime resolution is
# needed here -- just don't hardcode a card number.
INPUT_DEVICE_ARECORD = "plughw:wm8960soundcard,0"
OUTPUT_DEVICE_NAME = "Loopback"
OUTPUT_DEVICE_INDEX = 1  # the loopback's second endpoint -- see pairing note above


def find_output_device_index(name_substring, device_index):
    pattern = re.compile(re.escape(name_substring) + r".*\(hw:\d+," + str(device_index) + r"\)\s*$")
    for i, d in enumerate(sd.query_devices()):
        if pattern.search(d["name"]) and d["max_output_channels"] > 0:
            return i
    raise RuntimeError(f"no output device found matching {name_substring!r} device {device_index}")


CAPTURE_DIR = Path("/home/msenese/trigger-captures")
MAX_CAPTURES = 50

_buffer = np.zeros(BUFFER_SAMPLES, dtype=np.int16)
_write_pos = 0
_buffer_lock = threading.Lock()

# The real hardware clock (input) and the loopback's software clock (output)
# aren't synchronized, so a bounded queue with drop-oldest-on-full and
# silence-on-empty is the right tradeoff -- occasional single-block glitches
# under clock drift, never an unbounded backlog or a blocked audio thread.
_forward_queue = queue.Queue(maxsize=30)  # ~3s of cushion at 100ms blocks

_last_input_time = None
_last_output_time = None
_input_restart_count = 0
_output_status_count = 0

app = Flask(__name__)


def _handle_input_block(mono):
    global _write_pos, _last_input_time
    _last_input_time = time.time()

    with _buffer_lock:
        end_pos = _write_pos + len(mono)
        if end_pos <= BUFFER_SAMPLES:
            _buffer[_write_pos:end_pos] = mono
        else:
            first_part = BUFFER_SAMPLES - _write_pos
            _buffer[_write_pos:] = mono[:first_part]
            _buffer[:end_pos - BUFFER_SAMPLES] = mono[first_part:]
        _write_pos = end_pos % BUFFER_SAMPLES

    try:
        _forward_queue.put_nowait(mono.copy())
    except queue.Full:
        try:
            _forward_queue.get_nowait()
            _forward_queue.put_nowait(mono.copy())
        except queue.Empty:
            pass


def input_reader_thread():
    """Reads raw PCM from a continuous arecord subprocess (see the module
    docstring for why this isn't sounddevice like the output side is), and
    restarts arecord if it ever dies rather than leaving input silently dead."""
    global _input_restart_count
    backoff = 1
    max_backoff = 30
    while True:
        started_at = time.time()
        proc = subprocess.Popen(
            [
                "arecord", "-D", INPUT_DEVICE_ARECORD,
                "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
                "-t", "raw", "-q",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            while True:
                raw = proc.stdout.read(BLOCK_BYTES)
                if len(raw) < BLOCK_BYTES:
                    stderr = proc.stderr.read().decode(errors="replace")
                    print(f"[AUDIO] arecord input ended unexpectedly: {stderr[:300]}")
                    break
                mono = np.frombuffer(raw, dtype=np.int16)
                _handle_input_block(mono)
        finally:
            proc.kill()
            proc.wait()
        _input_restart_count += 1
        # A run that lasted a while was a real (if rare) hiccup, not a persistent
        # failure -- reset the backoff so we don't punish future one-off blips.
        ran_for = time.time() - started_at
        backoff = 1 if ran_for > 5 else min(backoff * 2, max_backoff)
        print(f"[AUDIO] Restarting arecord input in {backoff}s (restart #{_input_restart_count}, ran for {ran_for:.1f}s)")
        time.sleep(backoff)


def output_callback(outdata, frames, time_info, status):
    global _last_output_time, _output_status_count
    if status:
        _output_status_count += 1
        print(f"[AUDIO] Output status: {status}")
    _last_output_time = time.time()
    try:
        data = _forward_queue.get_nowait()
    except queue.Empty:
        outdata[:, 0] = 0
        return
    n = min(len(data), frames)
    outdata[:n, 0] = data[:n]
    if n < frames:
        outdata[n:, 0] = 0


def get_buffer_snapshot():
    with _buffer_lock:
        return np.concatenate([_buffer[_write_pos:], _buffer[:_write_pos]]).copy()


def label_to_slug(label):
    return label.replace(" ", "-")


def enforce_max_captures():
    files = sorted(CAPTURE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    while len(files) > MAX_CAPTURES:
        files.pop(0).unlink(missing_ok=True)


@app.route("/health")
def health():
    now = time.time()
    return jsonify({
        "input_alive": _last_input_time is not None and (now - _last_input_time) < 2,
        "output_alive": _last_output_time is not None and (now - _last_output_time) < 2,
        "forward_queue_size": _forward_queue.qsize(),
        "input_restart_count": _input_restart_count,
        "output_status_events": _output_status_count,
    })


@app.route("/capture", methods=["POST"])
def capture():
    body = request.get_json(silent=True) or {}
    label = body.get("label", "unknown")
    try:
        score = float(body.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    filename = f"{timestamp}-{label_to_slug(label)}-{score:.2f}.wav"
    path = CAPTURE_DIR / filename

    samples = get_buffer_snapshot()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())

    enforce_max_captures()
    return jsonify({"filename": filename})


@app.route("/captures")
def list_captures():
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(CAPTURE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify({
        "captures": [
            {"filename": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
            for f in files
        ]
    })


@app.route("/captures/<path:filename>", methods=["DELETE"])
def delete_capture(filename):
    if Path(filename).name != filename:
        abort(400)
    path = CAPTURE_DIR / filename
    if not path.is_file():
        abort(404)
    path.unlink()
    return jsonify({"deleted": filename})


if __name__ == "__main__":
    output_device_index = find_output_device_index(OUTPUT_DEVICE_NAME, OUTPUT_DEVICE_INDEX)
    print(f"[AUDIO] Forwarding output device: {sd.query_devices(output_device_index)['name']}")

    output_stream = sd.OutputStream(
        device=output_device_index,
        channels=CHANNELS,
        samplerate=SAMPLE_RATE,
        dtype="int16",
        blocksize=BLOCK_SIZE,
        callback=output_callback,
    )
    output_stream.start()

    threading.Thread(target=input_reader_thread, daemon=True).start()

    app.run(host="0.0.0.0", port=8081)
