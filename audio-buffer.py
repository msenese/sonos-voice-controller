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

# NOT CURRENTLY SAFE TO ENABLE. edge-impulse-linux-runner captures audio via
# sox grabbing its configured hardware device directly and exclusively --
# it never goes through ALSA's dsnoop/plug sharing layer, regardless of
# device name. ei-runner.service is pinned to --microphone hw:1,0 (see
# services/ei-runner.service); this service using "capture" (the shared
# plug+dsnoop PCM defined in /etc/asound.conf) will still collide with it
# for the same underlying hw:1,0 slave and take voice control down, exactly
# as it did in production. A validated fix exists -- ei-runner reads from an
# snd-aloop loopback device that this service forwards real audio into --
# but the duplex forwarding logic isn't implemented yet (see tasks 34/35).
# Do not enable audio-buffer.service until that's built and soak-tested.
DEVICE = "capture"

CAPTURE_DIR = Path("/home/msenese/trigger-captures")
MAX_CAPTURES = 50

_buffer = np.zeros(BUFFER_SAMPLES, dtype=np.int16)
_write_pos = 0
_buffer_lock = threading.Lock()

app = Flask(__name__)


def audio_callback(indata, frames, time_info, status):
    global _write_pos
    if status:
        print(f"[AUDIO] Status: {status}")
    mono = indata[:, 0] if indata.ndim > 1 else indata
    n = len(mono)
    with _buffer_lock:
        end_pos = _write_pos + n
        if end_pos <= BUFFER_SAMPLES:
            _buffer[_write_pos:end_pos] = mono
        else:
            first_part = BUFFER_SAMPLES - _write_pos
            _buffer[_write_pos:] = mono[:first_part]
            _buffer[:end_pos - BUFFER_SAMPLES] = mono[first_part:]
        _write_pos = end_pos % BUFFER_SAMPLES


def get_buffer_snapshot():
    with _buffer_lock:
        return np.concatenate([_buffer[_write_pos:], _buffer[:_write_pos]]).copy()


def label_to_slug(label):
    return label.replace(" ", "-")


def enforce_max_captures():
    files = sorted(CAPTURE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    while len(files) > MAX_CAPTURES:
        files.pop(0).unlink(missing_ok=True)


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
    stream = sd.InputStream(
        device=DEVICE,
        channels=CHANNELS,
        samplerate=SAMPLE_RATE,
        dtype="int16",
        blocksize=BLOCK_SIZE,
        callback=audio_callback,
    )
    stream.start()
    app.run(host="0.0.0.0", port=8081)
