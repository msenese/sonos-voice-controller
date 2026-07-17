import importlib
import io
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import date
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import config as cfg  # noqa: E402

STATE_FILE = Path("/tmp/sonos_controller_state.json")
TRAINING_DIR = PROJECT_ROOT / "training_samples"
CONFIG_PATH = PROJECT_ROOT / "config.py"
AUDIO_DEVICE = "plughw:1,0"

LIVE_MODEL_PATH = PROJECT_ROOT / "sonos-model.eim"
MODEL_BACKUP_PATH = PROJECT_ROOT / "sonos-model-previous.eim"
PENDING_MODEL_PATH = PROJECT_ROOT / "sonos-model-pending.eim"
SAMPLE_BASELINE_PATH = PROJECT_ROOT / "sample_counts_baseline.json"
GIT_ARCHIVE_REPO = Path.home() / "git-archive" / "sonos-voice-controller"
CAPTURE_DIR = Path("/home/msenese/trigger-captures")
AUDIO_BUFFER_API = "http://localhost:8081"

EI_RUNNER_SERVICE_PATH = Path("/etc/systemd/system/ei-runner.service")
EI_RUNNER_PENDING_PATH = PROJECT_ROOT / "ei-runner.service.pending"
CLASSIC_MICROPHONE = "hw:1,0"
BUFFER_MICROPHONE = "hw:2,0"
EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
EI_BUILD_TARGET = "runner-linux-aarch64"
EI_BUILD_ENGINE = "tflite"

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


def ei_configured():
    key = getattr(cfg, "EI_API_KEY", None)
    project_id = getattr(cfg, "EI_PROJECT_ID", None)
    return (
        bool(key) and key != "your-edge-impulse-api-key-here"
        and bool(project_id) and project_id != "your-project-id-here"
    )


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def ei_admin_configured():
    key = getattr(cfg, "EI_ADMIN_API_KEY", None)
    return bool(key) and key != "your-edge-impulse-admin-api-key-here"


def ei_admin_headers():
    return {"x-api-key": cfg.EI_ADMIN_API_KEY}


class EIError(Exception):
    pass


def ei_json(response):
    if "json" not in response.headers.get("Content-Type", ""):
        raise EIError(f"Edge Impulse API error (HTTP {response.status_code}): {response.text[:300]}")
    data = response.json()
    if data.get("success") is False:
        raise EIError(data.get("error", f"Edge Impulse API call failed (HTTP {response.status_code})"))
    return data


def fetch_sample_counts():
    counts = {}
    for label in LABELS:
        data = ei_json(requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/count",
            headers=ei_headers(),
            params={"category": "all", "labels": json.dumps([label])},
            timeout=15,
        ))
        counts[label] = data["count"]
    return counts


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


_active_recording = {"proc": None, "filename": None, "label": None, "duration": None, "capture_duration": None}
PREROLL_SECONDS = 1


def _resume_listening_services():
    subprocess.run(
        ["sudo", "systemctl", "start", "ei-runner.service"],
        capture_output=True, text=True, timeout=15,
    )
    # sonos-controller.service Requires=ei-runner.service, so stopping
    # ei-runner also stops it; starting ei-runner back up does not bring
    # it back automatically, so restart it explicitly too.
    subprocess.run(
        ["sudo", "systemctl", "restart", "sonos-controller.service"],
        capture_output=True, text=True, timeout=15,
    )


@app.route("/api/train/record/start", methods=["POST"])
def api_train_record_start():
    if _active_recording["proc"] is not None:
        return jsonify({"error": "a recording is already in progress"}), 409

    body = request.get_json(silent=True) or {}
    label = body.get("label")
    if label not in LABELS:
        return jsonify({"error": f"label must be one of {LABELS}"}), 400
    try:
        duration = float(body.get("duration", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid duration"}), 400
    if not (1 <= duration <= 20):
        return jsonify({"error": "duration must be between 1 and 20 seconds"}), 400

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{label_to_slug(label)}__{int(time.time())}.wav"
    path = TRAINING_DIR / filename

    # ei-runner.service holds the mic open continuously, so arecord can't
    # also open it. Stop it for the duration of the recording, then always
    # bring it back once /finish is called.
    stop_result = subprocess.run(
        ["sudo", "systemctl", "stop", "ei-runner.service"],
        capture_output=True, text=True, timeout=15,
    )
    if stop_result.returncode != 0:
        return jsonify({"error": f"could not stop ei-runner.service: {stop_result.stderr.strip()}"}), 500

    # A silent pre-roll absorbs the network round-trip + human reaction time
    # between "recording started" and the user actually speaking, so the
    # start of the word never gets clipped.
    capture_duration = round(duration) + PREROLL_SECONDS
    try:
        proc = subprocess.Popen(
            [
                "arecord", "-D", AUDIO_DEVICE,
                "-f", "S16_LE", "-c", "1", "-r", "16000",
                "-d", str(capture_duration), str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        _resume_listening_services()
        return jsonify({"error": f"could not run arecord: {e}"}), 500

    _active_recording.update({
        "proc": proc, "filename": filename, "label": label,
        "duration": duration, "capture_duration": capture_duration,
    })
    return jsonify({"filename": filename, "label": label, "duration": duration, "preroll": PREROLL_SECONDS})


@app.route("/api/train/record/finish", methods=["POST"])
def api_train_record_finish():
    proc = _active_recording["proc"]
    if proc is None:
        return jsonify({"error": "no recording in progress"}), 400

    duration = _active_recording["duration"]
    capture_duration = _active_recording["capture_duration"]
    filename = _active_recording["filename"]
    label = _active_recording["label"]

    try:
        _, stderr = proc.communicate(timeout=capture_duration + 10)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
        returncode = -1
    finally:
        _resume_listening_services()
        _active_recording.update({
            "proc": None, "filename": None, "label": None,
            "duration": None, "capture_duration": None,
        })

    if returncode != 0:
        return jsonify({"error": (stderr or "arecord failed").strip()}), 500
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

    response = upload_wav_to_ei(path, filename, label)
    if response.status_code >= 300:
        return jsonify({"error": f"Edge Impulse upload failed: {response.status_code} {response.text}"}), 502

    return jsonify({"uploaded": filename, "label": label})


@app.route("/training-samples/<path:filename>")
def training_sample_file(filename):
    return send_from_directory(TRAINING_DIR, filename)


def upload_wav_to_ei(path, filename, label):
    with path.open("rb") as f:
        return requests.post(
            "https://ingestion.edgeimpulse.com/api/training/files",
            headers={
                "x-api-key": cfg.EI_API_KEY,
                "x-label": label,
            },
            files={"data": (filename, f, "audio/wav")},
            timeout=30,
        )


@app.route("/trigger-captures/<path:filename>")
def trigger_capture_file(filename):
    return send_from_directory(CAPTURE_DIR, filename)


def parse_capture_filename(filename):
    base = filename[:-4] if filename.endswith(".wav") else filename
    parts = base.split("-")
    rest = parts[6:]  # drop the 6-part YYYY-MM-DD-HH-MM-SS timestamp
    return " ".join(rest[:-1])  # drop the trailing score


@app.route("/api/captures")
def api_captures():
    try:
        r = requests.get(f"{AUDIO_BUFFER_API}/captures", timeout=10)
    except requests.RequestException as e:
        return jsonify({"error": f"could not reach audio-buffer service: {e}"}), 502
    if r.status_code >= 300:
        return jsonify({"error": f"audio-buffer service error: {r.status_code} {r.text}"}), 502
    return jsonify(r.json())


@app.route("/api/captures/<path:filename>", methods=["DELETE"])
def api_captures_delete(filename):
    if Path(filename).name != filename:
        return jsonify({"error": "invalid filename"}), 400
    try:
        r = requests.delete(f"{AUDIO_BUFFER_API}/captures/{filename}", timeout=10)
    except requests.RequestException as e:
        return jsonify({"error": f"could not reach audio-buffer service: {e}"}), 502
    if r.status_code >= 300:
        return jsonify({"error": f"audio-buffer service error: {r.status_code} {r.text}"}), 502
    return jsonify(r.json())


def _upload_capture_and_remove(filename, label):
    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        return jsonify({"error": "EI_API_KEY is not configured in config.py"}), 400
    if Path(filename).name != filename:
        return jsonify({"error": "invalid filename"}), 400
    if label not in LABELS:
        return jsonify({"error": f"label must be one of {LABELS}"}), 400

    path = CAPTURE_DIR / filename
    if not path.is_file():
        return jsonify({"error": "file not found"}), 404

    response = upload_wav_to_ei(path, filename, label)
    if response.status_code >= 300:
        return jsonify({"error": f"Edge Impulse upload failed: {response.status_code} {response.text}"}), 502

    try:
        requests.delete(f"{AUDIO_BUFFER_API}/captures/{filename}", timeout=10)
    except requests.RequestException as e:
        return jsonify({"uploaded": filename, "label": label, "deleted": False, "delete_error": str(e)})

    return jsonify({"uploaded": filename, "label": label, "deleted": True})


@app.route("/api/captures/<path:filename>/confirm", methods=["POST"])
def api_captures_confirm(filename):
    label = parse_capture_filename(filename)
    return _upload_capture_and_remove(filename, label)


@app.route("/api/captures/<path:filename>/relabel", methods=["POST"])
def api_captures_relabel(filename):
    body = request.get_json(silent=True) or {}
    label = body.get("label", "")
    return _upload_capture_and_remove(filename, label)


@app.route("/api/train/samples/lookup")
def api_train_samples_lookup():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    filename = request.args.get("filename", "")
    if not filename or Path(filename).name != filename:
        return jsonify({"error": "invalid filename"}), 400

    # Edge Impulse stores the filename without its extension.
    filename_stem = Path(filename).stem

    try:
        data = ei_json(requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "filename": filename_stem},
            timeout=15,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502

    samples = data.get("samples", [])
    if not samples:
        return jsonify({"error": "sample not found on Edge Impulse yet (upload may still be processing)"}), 404
    match = next((s for s in samples if s.get("filename") == filename_stem), samples[0])
    return jsonify({"sample_id": match["id"]})


@app.route("/api/train/samples/<int:sample_id>/find-segments", methods=["POST"])
def api_train_find_segments(sample_id):
    if not ei_admin_configured():
        return jsonify({"error": "EI_ADMIN_API_KEY not configured in config.py (find-segments requires an Admin-role key)"}), 400
    body = request.get_json(silent=True) or {}
    try:
        segment_length_ms = int(body.get("segment_length_ms", 1000))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid segment_length_ms"}), 400
    if not (100 <= segment_length_ms <= 10000):
        return jsonify({"error": "segment_length_ms must be between 100 and 10000"}), 400

    try:
        data = ei_json(requests.post(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}/find-segments",
            headers=ei_admin_headers(),
            json={"shiftSegments": bool(body.get("shift_segments", False)), "segmentLengthMs": segment_length_ms},
            timeout=30,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"segments": data.get("segments", [])})


@app.route("/api/train/samples/<int:sample_id>/segment", methods=["POST"])
def api_train_segment(sample_id):
    if not ei_admin_configured():
        return jsonify({"error": "EI_ADMIN_API_KEY not configured in config.py (segment requires an Admin-role key)"}), 400
    body = request.get_json(silent=True) or {}
    segments = body.get("segments")
    if not isinstance(segments, list) or not segments:
        return jsonify({"error": "segments must be a non-empty list"}), 400
    for seg in segments:
        if not isinstance(seg, dict) or "startMs" not in seg or "endMs" not in seg:
            return jsonify({"error": "each segment needs startMs and endMs"}), 400

    try:
        ei_json(requests.post(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}/segment",
            headers=ei_admin_headers(),
            json={"segments": segments},
            timeout=30,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502

    # The segment API's docs claim the original sample is auto-deleted, but in
    # practice it stays fully active (isDisabled: False) unless removed here,
    # which would otherwise pollute training with the raw multi-word clip.
    original_deleted = True
    try:
        ei_json(requests.delete(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}",
            headers=ei_admin_headers(),
            timeout=15,
        ))
    except EIError:
        original_deleted = False

    # Its segments now live on Edge Impulse, so the raw local copy on the Pi
    # has no further purpose.
    filename = body.get("filename", "")
    if filename and Path(filename).name == filename:
        (TRAINING_DIR / filename).unlink(missing_ok=True)

    return jsonify({"split": len(segments), "original_deleted": original_deleted})


@app.route("/api/model/sample-counts")
def api_model_sample_counts():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    try:
        counts = fetch_sample_counts()
    except EIError as e:
        return jsonify({"error": str(e)}), 502

    baseline = {}
    baseline_at = None
    if SAMPLE_BASELINE_PATH.exists():
        try:
            snapshot = json.loads(SAMPLE_BASELINE_PATH.read_text())
            baseline = snapshot.get("counts", {})
            baseline_at = snapshot.get("snapshot_at")
        except (OSError, ValueError):
            pass

    return jsonify({
        "counts": {
            label: {"total": count, "new": count - baseline.get(label, count)}
            for label, count in counts.items()
        },
        "baseline_at": baseline_at,
    })


@app.route("/api/model/sample-counts/snapshot", methods=["POST"])
def api_model_sample_counts_snapshot():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    try:
        counts = fetch_sample_counts()
    except EIError as e:
        return jsonify({"error": str(e)}), 502

    snapshot = {"counts": counts, "snapshot_at": time.time()}
    SAMPLE_BASELINE_PATH.write_text(json.dumps(snapshot))
    return jsonify(snapshot)


@app.route("/api/model/retrain/start", methods=["POST"])
def api_model_retrain_start():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    if not ei_admin_configured():
        return jsonify({"error": "EI_ADMIN_API_KEY not configured in config.py (retrain requires an Admin-role key)"}), 400
    try:
        data = ei_json(requests.post(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/jobs/retrain",
            headers=ei_admin_headers(), timeout=15,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"job_id": data["id"]})


@app.route("/api/model/build/start", methods=["POST"])
def api_model_build_start():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    try:
        data = ei_json(requests.post(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/jobs/build-ondevice-model",
            headers=ei_headers(),
            params={"type": EI_BUILD_TARGET},
            json={"engine": EI_BUILD_ENGINE},
            timeout=15,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"job_id": data["id"]})


@app.route("/api/model/job/<int:job_id>/status")
def api_model_job_status(job_id):
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    try:
        data = ei_json(requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/jobs/{job_id}/status",
            headers=ei_headers(), timeout=15,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502
    job = data.get("job", {})
    return jsonify({
        "finished": bool(job.get("finished")),
        "finishedSuccessful": job.get("finishedSuccessful"),
    })


@app.route("/api/model/metrics")
def api_model_metrics():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    try:
        impulse_data = ei_json(requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/impulse",
            headers=ei_headers(), timeout=15,
        ))
        learn_blocks = impulse_data.get("impulse", {}).get("learnBlocks", [])
        keras_block = next((b for b in learn_blocks if b.get("type") == "keras"), None)
        if not keras_block:
            return jsonify({"error": "no keras learn block found on this impulse"}), 404

        metadata = ei_json(requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/training/keras/{keras_block['id']}/metadata",
            headers=ei_headers(), timeout=15,
        ))
    except EIError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({
        "classNames": metadata.get("classNames", []),
        "modelValidationMetrics": metadata.get("modelValidationMetrics", []),
    })


@app.route("/api/model/download", methods=["POST"])
def api_model_download():
    if not ei_configured():
        return jsonify({"error": "EI_API_KEY / EI_PROJECT_ID not configured in config.py"}), 400
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/deployment/download",
        headers=ei_headers(),
        params={"type": EI_BUILD_TARGET},
        timeout=120,
    )
    if r.status_code != 200:
        return jsonify({"error": f"download failed (HTTP {r.status_code}): {r.text[:300]}"}), 502

    if r.content[:2] == b"PK":
        # Some deployment targets (e.g. C++ library) return a zip; the EIM
        # runner target returns the raw binary directly (checked below).
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        eim_names = [n for n in zf.namelist() if n.endswith(".eim")]
        if not eim_names:
            return jsonify({"error": "no .eim file found in deployment zip"}), 502
        data = zf.read(eim_names[0])
    elif r.content[:4] == b"\x7fELF":
        data = r.content
    else:
        return jsonify({"error": f"unrecognized deployment response format (first bytes: {r.content[:16]!r})"}), 502

    PENDING_MODEL_PATH.write_bytes(data)
    PENDING_MODEL_PATH.chmod(0o755)
    return jsonify({"size": len(data)})


@app.route("/api/model/pending")
def api_model_pending():
    if not PENDING_MODEL_PATH.exists():
        return jsonify({"exists": False})
    st = PENDING_MODEL_PATH.stat()
    return jsonify({"exists": True, "size": st.st_size, "mtime": st.st_mtime})


def archive_model_to_git(accuracy=None):
    if not GIT_ARCHIVE_REPO.exists():
        return {"archived": False, "error": "git archive clone not found on the Pi"}

    models_dir = GIT_ARCHIVE_REPO / "models"
    models_dir.mkdir(exist_ok=True)
    today = date.today().isoformat()
    dated_name = f"sonos-model-{today}.eim"

    try:
        data = LIVE_MODEL_PATH.read_bytes()
        (models_dir / dated_name).write_bytes(data)
        (models_dir / "sonos-model-current.eim").write_bytes(data)

        def run(*args, **kwargs):
            return subprocess.run(
                ["git", "-C", str(GIT_ARCHIVE_REPO), *args],
                capture_output=True, text=True, timeout=kwargs.get("timeout", 20),
            )

        pull = run("pull", "--ff-only", timeout=30)
        if pull.returncode != 0:
            return {"archived": False, "error": f"git pull failed: {pull.stderr.strip()}"}

        run("add", f"models/{dated_name}", "models/sonos-model-current.eim")

        acc_str = f" - {accuracy:.1f}% accuracy" if accuracy is not None else ""
        commit = run("commit", "-m", f"Auto-archive model activated {today}{acc_str}")
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            return {"archived": False, "error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"}

        push = run("push", timeout=30)
        if push.returncode != 0:
            return {"archived": False, "error": f"git push failed: {push.stderr.strip()}"}

        return {"archived": True, "filename": dated_name}
    except OSError as e:
        return {"archived": False, "error": str(e)}


@app.route("/api/model/activate", methods=["POST"])
def api_model_activate():
    if not PENDING_MODEL_PATH.exists():
        return jsonify({"error": "no pending model to activate"}), 400

    if LIVE_MODEL_PATH.exists():
        MODEL_BACKUP_PATH.write_bytes(LIVE_MODEL_PATH.read_bytes())
    PENDING_MODEL_PATH.replace(LIVE_MODEL_PATH)

    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "ei-runner.service"],
            capture_output=True, text=True, timeout=30,
        )
    except OSError as e:
        return jsonify({"error": f"model file swapped, but restart failed: {e}"}), 500
    if result.returncode != 0:
        return jsonify({"error": f"model file swapped, but restart failed: {result.stderr.strip()}"}), 500

    body = request.get_json(silent=True) or {}
    accuracy = body.get("accuracy")
    archive_result = archive_model_to_git(accuracy if isinstance(accuracy, (int, float)) else None)

    return jsonify({"activated": True, "archive": archive_result})


def _build_ei_runner_unit(microphone):
    return f"""[Unit]
Description=Edge Impulse Linux Runner
After=network.target sound.target

[Service]
User=msenese
ExecStart=/usr/bin/edge-impulse-linux-runner --microphone {microphone} --model-file /home/msenese/sonos-model.eim --disable-camera
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _run_sudo(*args, timeout=15):
    result = subprocess.run(["sudo", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")


def _wait_for_audio_buffer_input(timeout=15, interval=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{AUDIO_BUFFER_API}/health", timeout=3)
            if r.json().get("input_alive"):
                return True
        except requests.RequestException:
            pass
        time.sleep(interval)
    return False


def _wait_for_ei_connection(timeout=40, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("connection_status") == "connected":
                return True
        except (OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def _switch_audio_mode_raw(mode):
    microphone = BUFFER_MICROPHONE if mode == "buffer" else CLASSIC_MICROPHONE

    _run_sudo("systemctl", "stop", "ei-runner.service")

    if mode == "buffer":
        _run_sudo("systemctl", "enable", "audio-buffer.service")
        _run_sudo("systemctl", "start", "audio-buffer.service")
        if not _wait_for_audio_buffer_input(timeout=15):
            raise RuntimeError("audio-buffer.py did not lock onto the microphone in time")
    else:
        _run_sudo("systemctl", "stop", "audio-buffer.service")
        _run_sudo("systemctl", "disable", "audio-buffer.service")
        # Auto-Resume Playback's toggle only exists in the UI while Capture Mode
        # is On -- don't leave it silently enabled with no visible way to turn
        # it off once we're switching Off.
        global _auto_resume_enabled
        with _auto_resume_lock:
            _auto_resume_enabled = False

    EI_RUNNER_PENDING_PATH.write_text(_build_ei_runner_unit(microphone))
    _run_sudo("cp", str(EI_RUNNER_PENDING_PATH), str(EI_RUNNER_SERVICE_PATH))
    _run_sudo("systemctl", "daemon-reload")
    _run_sudo("systemctl", "start", "ei-runner.service")
    _run_sudo("systemctl", "restart", "sonos-controller.service")

    return _wait_for_ei_connection(timeout=40)


@app.route("/api/audio-mode")
def api_audio_mode_get():
    try:
        content = EI_RUNNER_SERVICE_PATH.read_text()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    mode = "buffer" if BUFFER_MICROPHONE in content else "classic"
    audio_buffer_active = subprocess.run(
        ["systemctl", "is-active", "audio-buffer.service"],
        capture_output=True, text=True,
    ).stdout.strip() == "active"
    return jsonify({"mode": mode, "audio_buffer_active": audio_buffer_active})


@app.route("/api/audio-mode", methods=["POST"])
def api_audio_mode_post():
    body = request.get_json(silent=True) or {}
    target_mode = body.get("mode")
    if target_mode not in ("classic", "buffer"):
        return jsonify({"error": "mode must be 'classic' or 'buffer'"}), 400

    try:
        connected = _switch_audio_mode_raw(target_mode)
    except Exception as e:
        rolled_back = False
        if target_mode != "classic":
            try:
                _switch_audio_mode_raw("classic")
                rolled_back = True
            except Exception:
                pass
        return jsonify({"error": str(e), "rolled_back": rolled_back}), 500

    if not connected and target_mode != "classic":
        rolled_back = False
        try:
            _switch_audio_mode_raw("classic")
            rolled_back = True
        except Exception:
            pass
        return jsonify({
            "error": f"switched to {target_mode} but ei-runner never reconnected",
            "rolled_back": rolled_back,
        }), 500

    return jsonify({"mode": target_mode, "connected": connected})


def ha_headers():
    return {
        "Authorization": f"Bearer {cfg.HA_TOKEN}",
        "Content-Type": "application/json",
    }


def ha_call_service(domain, service, data):
    try:
        r = requests.post(
            f"{cfg.HA_URL}/api/services/{domain}/{service}",
            headers=ha_headers(),
            json=data,
            timeout=5,
        )
    except requests.RequestException as e:
        return str(e)
    if r.status_code >= 300:
        return f"HA error {r.status_code}: {r.text[:200]}"
    return None


@app.route("/api/sonos/state")
def api_sonos_state():
    try:
        r = requests.get(f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=ha_headers(), timeout=5)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502
    if r.status_code >= 300:
        return jsonify({"error": f"HA error {r.status_code}: {r.text[:200]}"}), 502
    data = r.json()
    attrs = data.get("attributes", {})
    return jsonify({
        "state": data.get("state"),
        "volume_level": attrs.get("volume_level"),
        "is_volume_muted": bool(attrs.get("is_volume_muted", False)),
    })


@app.route("/api/sonos/play", methods=["POST"])
def api_sonos_play():
    err = ha_call_service("media_player", "media_play", {"entity_id": cfg.SONOS_ENTITY})
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True})


@app.route("/api/sonos/pause", methods=["POST"])
def api_sonos_pause():
    err = ha_call_service("media_player", "media_pause", {"entity_id": cfg.SONOS_ENTITY})
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True})


@app.route("/api/sonos/mute", methods=["POST"])
def api_sonos_mute():
    body = request.get_json(silent=True) or {}
    muted = bool(body.get("muted"))
    err = ha_call_service("media_player", "volume_mute", {"entity_id": cfg.SONOS_ENTITY, "is_volume_muted": muted})
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True})


@app.route("/api/sonos/volume", methods=["POST"])
def api_sonos_volume():
    body = request.get_json(silent=True) or {}
    try:
        level = float(body.get("level"))
    except (TypeError, ValueError):
        return jsonify({"error": "level must be a number"}), 400
    level = max(0.0, min(1.0, level))
    err = ha_call_service("media_player", "volume_set", {"entity_id": cfg.SONOS_ENTITY, "volume_level": level})
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True})


# "Auto-Resume Playback": for gathering trigger-capture samples without having
# to manually hit play after every false pause trigger. Off by default -- this
# is a manual testing aid, not something that should run unattended.
_auto_resume_enabled = False
_auto_resume_lock = threading.Lock()


def _auto_resume_loop():
    while True:
        time.sleep(1)
        with _auto_resume_lock:
            enabled = _auto_resume_enabled
        if not enabled:
            continue
        try:
            r = requests.get(f"{cfg.HA_URL}/api/states/{cfg.SONOS_ENTITY}", headers=ha_headers(), timeout=5)
            if r.status_code < 300 and r.json().get("state") == "paused":
                ha_call_service("media_player", "media_play", {"entity_id": cfg.SONOS_ENTITY})
        except requests.RequestException:
            pass


@app.route("/api/sonos/auto-resume")
def api_sonos_auto_resume_get():
    return jsonify({"enabled": _auto_resume_enabled})


@app.route("/api/sonos/auto-resume", methods=["POST"])
def api_sonos_auto_resume_post():
    global _auto_resume_enabled
    body = request.get_json(silent=True) or {}
    with _auto_resume_lock:
        _auto_resume_enabled = bool(body.get("enabled"))
    return jsonify({"enabled": _auto_resume_enabled})


if __name__ == "__main__":
    threading.Thread(target=_auto_resume_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
