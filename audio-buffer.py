import io
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
from flask import Flask, Response, abort, jsonify, request

SAMPLE_RATE = 16000
CHANNELS = 1
# In Buffer mode, the trigger only fires after audio has gone through the
# full forwarding+detection relay (block chunking -> queue -> ALSA loopback
# -> sox -> ei-runner's inference window -> CONSECUTIVE_REQUIRED consecutive
# hits) -- much slower than a direct mic read. This ring buffer, though,
# keeps recording from the real mic in real time the whole time. A short
# buffer (previously 1.3s, tuned for Classic mode's near-zero relay latency)
# can fully wrap past the actual trigger phrase before /capture is ever
# called, so the snapshot comes back as background noise or just the tail
# end. Sized generously here so the phrase is still in the window even
# with multiple seconds of relay latency -- memory cost is trivial either
# way (a few hundred KB of int16 samples).
BUFFER_SECONDS = 4.0
BUFFER_SAMPLES = int(SAMPLE_RATE * BUFFER_SECONDS)
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
MAX_CAPTURES = 200

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
_output_pull_count = 0
_output_miss_count = 0

# Forward-looking capture (e.g. "record the next N seconds of a spoken
# command after a wake trigger") -- distinct from the ring buffer above,
# which only looks backward. Each in-progress /capture-forward request
# registers a callback here; _handle_input_block feeds it live blocks as
# they arrive, same as it already feeds the ring buffer and forward queue.
# A list rather than a single slot so overlapping requests don't clobber
# each other.
_forward_capture_listeners = []
_forward_capture_lock = threading.Lock()

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

    with _forward_capture_lock:
        listeners = list(_forward_capture_listeners)
    for listener in listeners:
        listener(mono)

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
    global _last_output_time, _output_status_count, _output_pull_count, _output_miss_count
    if status:
        _output_status_count += 1
        print(f"[AUDIO] Output status: {status}")
    _last_output_time = time.time()
    try:
        data = _forward_queue.get_nowait()
        _output_pull_count += 1
    except queue.Empty:
        _output_miss_count += 1
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
        "output_pull_count": _output_pull_count,
        "output_miss_count": _output_miss_count,
        "output_miss_rate": round(_output_miss_count / max(1, _output_pull_count + _output_miss_count), 4),
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


MIN_FORWARD_CAPTURE_SECONDS = 0.5
MAX_FORWARD_CAPTURE_SECONDS = 15
DEFAULT_FORWARD_CAPTURE_SECONDS = 4.5


@app.route("/capture-forward", methods=["POST"])
def capture_forward():
    # Records the NEXT N seconds of live mic audio and returns it as a WAV,
    # for callers that need to capture what's said *after* some trigger
    # moment (e.g. a follow-up voice command) rather than a snapshot of
    # what's already in the ring buffer. Only meaningful while this service
    # owns the mic (Buffer mode) -- there's no other way to get a second,
    # independent capture stream without a conflicting second device open.
    body = request.get_json(silent=True) or {}
    try:
        duration_seconds = float(body.get("duration_seconds", DEFAULT_FORWARD_CAPTURE_SECONDS))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid duration_seconds"}), 400
    if not (MIN_FORWARD_CAPTURE_SECONDS <= duration_seconds <= MAX_FORWARD_CAPTURE_SECONDS):
        return jsonify({
            "error": f"duration_seconds must be between {MIN_FORWARD_CAPTURE_SECONDS} "
                     f"and {MAX_FORWARD_CAPTURE_SECONDS}"
        }), 400

    target_samples = int(SAMPLE_RATE * duration_seconds)
    collected = []
    collected_samples = 0
    done_event = threading.Event()

    def on_block(mono):
        nonlocal collected_samples
        collected.append(mono)
        collected_samples += len(mono)
        if collected_samples >= target_samples:
            done_event.set()

    with _forward_capture_lock:
        _forward_capture_listeners.append(on_block)
    try:
        # A generous few seconds of slack on top of the requested duration --
        # this should only ever fire if the input thread has actually died,
        # since normal operation delivers blocks every ~100ms.
        if not done_event.wait(timeout=duration_seconds + 5):
            return jsonify({"error": "timed out waiting for audio -- is the input stream alive?"}), 504
    finally:
        with _forward_capture_lock:
            _forward_capture_listeners.remove(on_block)

    samples = np.concatenate(collected)[:target_samples]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())
    return Response(buf.getvalue(), mimetype="audio/wav")


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

    # Input has to start (and build up a real backlog) BEFORE the output
    # stream starts, not after -- PortAudio requests several periods' worth
    # of data in a burst right when a stream starts, and starting output
    # first (the previous order) meant every one of those initial requests
    # hit an empty queue and got silence instead of real audio. Confirmed
    # directly: a standalone test of this exact queue+callback mechanism
    # showed 16 of 56 callback pulls (29%) landing on an empty queue when
    # output started before input was feeding it -- a strong candidate for
    # the dropped-audio/reduced-peak signal seen in Buffer mode's relayed
    # captures vs. a direct mic capture of the same moment.
    threading.Thread(target=input_reader_thread, daemon=True).start()
    while _forward_queue.qsize() < 10:
        time.sleep(0.05)
    print(f"[AUDIO] Input backlog ready ({_forward_queue.qsize()} blocks queued), starting output")

    output_stream = sd.OutputStream(
        device=output_device_index,
        channels=CHANNELS,
        samplerate=SAMPLE_RATE,
        dtype="int16",
        blocksize=BLOCK_SIZE,
        callback=output_callback,
    )
    output_stream.start()

    # threaded=True matters now beyond convenience: /capture-forward blocks
    # its own request thread for several seconds, and without this the
    # single-threaded dev server would stall /health, /capture, and
    # /captures (the dashboard's poll) for that whole window too.
    app.run(host="0.0.0.0", port=8081, threaded=True)
