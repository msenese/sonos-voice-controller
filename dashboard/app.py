import importlib
import re
import subprocess
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import config as cfg  # noqa: E402

STATE_FILE = Path("/tmp/sonos_controller_state.json")
TRAINING_DIR = PROJECT_ROOT / "training_samples"
CONFIG_PATH = PROJECT_ROOT / "config.py"
AUDIO_DEVICE = "hw:1,0"

LABELS = ["noise", "sonos pause", "sonos play", "unknown"]

CONFIG_FIELDS = {
    "THRESHOLD": {"type": float, "min": 0.0, "max": 1.0},
    "SONOS_PLAY_THRESHOLD": {"type": float, "min": 0.0, "max": 1.0},
    "COOLDOWN": {"type": float, "min": 0.0, "max": 30.0},
    "CONSECUTIVE_REQUIRED": {"type": int, "min": 1, "max": 10},
}

app = Flask(__name__)


def label_to_slug(label):
    return label.replace(" ", "_")


def slug_to_label(slug):
    return slug.replace("_", " ")


def reload_config():
    importlib.reload(cfg)


def read_state():
    if not STATE_FILE.exists():
        return {
            "scores": {},
            "history": [],
            "connection_status": "unknown",
            "muted": None,
            "updated_at": None,
        }
    try:
        import json
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {
            "scores": {},
            "history": [],
            "connection_status": "unknown",
            "muted": None,
            "updated_at": None,
        }


@app.route("/")
def index():
    return render_template("index.html", labels=LABELS)


@app.route("/api/state")
def api_state():
    state = read_state()
    state["config"] = {
        "threshold": cfg.THRESHOLD,
        "sonos_play_threshold": cfg.SONOS_PLAY_THRESHOLD,
        "cooldown": cfg.COOLDOWN,
        "consecutive_required": cfg.CONSECUTIVE_REQUIRED,
    }
    return jsonify(state)


@app.route("/api/config", methods=["POST"])
def api_config():
    body = request.get_json(silent=True) or {}
    updates = {}
    for key, spec in CONFIG_FIELDS.items():
        json_key = key.lower()
        if json_key not in body:
            continue
        try:
            value = spec["type"](body[json_key])
        except (TypeError, ValueError):
            return jsonify({"error": f"invalid value for {json_key}"}), 400
        if not (spec["min"] <= value <= spec["max"]):
            return jsonify({"error": f"{json_key} out of range"}), 400
        updates[key] = value

    if not updates:
        return jsonify({"error": "no valid config fields provided"}), 400

    text = CONFIG_PATH.read_text()
    for key, value in updates.items():
        pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
        text = pattern.sub(f"{key} = {value!r}", text)
    CONFIG_PATH.write_text(text)
    reload_config()

    return jsonify({
        "threshold": cfg.THRESHOLD,
        "sonos_play_threshold": cfg.SONOS_PLAY_THRESHOLD,
        "cooldown": cfg.COOLDOWN,
        "consecutive_required": cfg.CONSECUTIVE_REQUIRED,
    })


@app.route("/api/mic-level", methods=["POST"])
def api_mic_level():
    body = request.get_json(silent=True) or {}
    try:
        level = int(body.get("level"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid level"}), 400
    if not (0 <= level <= 63):
        return jsonify({"error": "level out of range (0-63)"}), 400

    try:
        result = subprocess.run(
            ["amixer", "-c", "1", "sset", "Capture", str(level)],
            capture_output=True, text=True,
        )
    except OSError as e:
        return jsonify({"error": f"could not run amixer: {e}"}), 500
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "amixer failed"}), 500
    return jsonify({"level": level})


@app.route("/api/system")
def api_system():
    uptime_seconds = None
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass

    cpu_temp_c = None
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        cpu_temp_c = int(raw) / 1000.0
    except (OSError, ValueError):
        pass

    mem_total_kb = mem_available_kb = None
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split(":")
            if len(parts) == 2:
                meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
        mem_total_kb = meminfo.get("MemTotal")
        mem_available_kb = meminfo.get("MemAvailable")
    except (OSError, ValueError):
        pass

    mem_used_kb = None
    if mem_total_kb is not None and mem_available_kb is not None:
        mem_used_kb = mem_total_kb - mem_available_kb

    return jsonify({
        "uptime_seconds": uptime_seconds,
        "cpu_temp_c": cpu_temp_c,
        "mem_total_kb": mem_total_kb,
        "mem_used_kb": mem_used_kb,
    })


@app.route("/api/train/record", methods=["POST"])
def api_train_record():
    body = request.get_json(silent=True) or {}
    label = body.get("label")
    if label not in LABELS:
        return jsonify({"error": f"label must be one of {LABELS}"}), 400
    try:
        duration = float(body.get("duration", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid duration"}), 400
    if not (1 <= duration <= 5):
        return jsonify({"error": "duration must be between 1 and 5 seconds"}), 400

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{label_to_slug(label)}__{int(time.time())}.wav"
    path = TRAINING_DIR / filename

    try:
        result = subprocess.run(
            [
                "arecord", "-D", AUDIO_DEVICE,
                "-f", "S16_LE", "-c", "1", "-r", "16000",
                "-d", str(duration), str(path),
            ],
            capture_output=True, text=True,
        )
    except OSError as e:
        return jsonify({"error": f"could not run arecord: {e}"}), 500
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "arecord failed"}), 500

    return jsonify({"filename": filename, "label": label, "duration": duration})


@app.route("/api/train/upload", methods=["POST"])
def api_train_upload():
    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        return jsonify({"error": "EI_API_KEY is not configured in config.py"}), 400

    body = request.get_json(silent=True) or {}
    filename = body.get("filename", "")
    # Reject anything that isn't a bare filename we generated ourselves.
    if not filename or Path(filename).name != filename:
        return jsonify({"error": "invalid filename"}), 400

    path = TRAINING_DIR / filename
    if not path.is_file():
        return jsonify({"error": "file not found"}), 404

    slug = filename.split("__", 1)[0]
    label = slug_to_label(slug)
    if label not in LABELS:
        return jsonify({"error": "could not determine label from filename"}), 400

    import requests
    with path.open("rb") as f:
        response = requests.post(
            "https://ingestion.edgeimpulse.com/api/training/files",
            headers={
                "x-api-key": cfg.EI_API_KEY,
                "x-label": label,
            },
            files={"data": (filename, f, "audio/wav")},
            timeout=30,
        )

    if response.status_code >= 300:
        return jsonify({"error": f"Edge Impulse upload failed: {response.status_code} {response.text}"}), 502

    return jsonify({"uploaded": filename, "label": label})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
