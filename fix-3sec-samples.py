#!/usr/bin/env python3
"""One-time cleanup: the trigger-capture ring buffer (audio-buffer.py)
uploaded 3-second clips before BUFFER_SECONDS was shortened, and 3s
samples are known to hurt the model (too much non-command audio mixed
into a single training example). This finds every currently-enabled
sample longer than ~2 seconds project-wide (not filtered by label --
same reasoning as cleanup-rp2040-samples.py: a hardcoded label list
would silently miss a label added later), splits each into clean,
non-overlapping 1.3s segments (discarding any leftover shorter than
1.3s -- no overlap, no padding), uploads each segment under the
original's label, and only then disables (never deletes) the original,
so it can be re-enabled if something looks wrong.

Segments are reconstructed from the sample's own `payload.values` (the
raw PCM Edge Impulse already returns from a plain GET on the sample --
no separate binary/wav download endpoint exists), not re-downloaded
from anywhere else.

Requires EI_API_KEY (read/upload) and EI_ADMIN_API_KEY (disable --
same admin-only pattern as the dashboard's existing sample-splitting
feature).

Usage: python3 fix-3sec-samples.py [--yes]
"""
import io
import struct
import sys
import wave

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
INGESTION_URL = "https://ingestion.edgeimpulse.com/api/training/files"
PAGE_SIZE = 100
MIN_LENGTH_MS = 2000
SEGMENT_SECONDS = 1.3
SAMPLE_RATE = 16000
SEGMENT_SAMPLES = int(SAMPLE_RATE * SEGMENT_SECONDS)


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def ei_admin_headers():
    return {"x-api-key": cfg.EI_ADMIN_API_KEY}


def list_long_samples():
    matches = []
    offset = 0
    while True:
        r = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        page = r.json().get("samples", [])
        for s in page:
            if s.get("totalLengthMs", 0) > MIN_LENGTH_MS and not s.get("isDisabled"):
                matches.append(s)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def fetch_sample_payload(sample_id):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}",
        headers=ei_headers(),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    values = [v[0] for v in data["payload"]["values"]]
    frequency = data["sample"]["frequency"]
    return values, frequency


def values_to_wav_bytes(values, frequency):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(frequency)
        wf.writeframes(struct.pack(f"<{len(values)}h", *values))
    return buf.getvalue()


def upload_segment(filename, wav_bytes, label):
    return requests.post(
        INGESTION_URL,
        headers={"x-api-key": cfg.EI_API_KEY, "x-label": label},
        files={"data": (filename, io.BytesIO(wav_bytes), "audio/wav")},
        timeout=30,
    )


def disable_sample(sample_id):
    return requests.post(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}/disable",
        headers=ei_admin_headers(),
        json={},
        timeout=15,
    )


def process_sample(sample):
    sample_id = sample["id"]
    filename = sample["filename"]
    label = sample["label"]
    length_ms = sample["totalLengthMs"]

    print(f"\n[{sample_id}] {filename} (label={label!r}, {length_ms}ms)")

    values, frequency = fetch_sample_payload(sample_id)
    if frequency != SAMPLE_RATE:
        print(f"  SKIPPED: sample rate is {frequency}Hz, expected {SAMPLE_RATE}Hz -- not safe to assume segment length")
        return {"sample_id": sample_id, "filename": filename, "status": "skipped", "reason": "unexpected sample rate"}

    num_segments = len(values) // SEGMENT_SAMPLES
    if num_segments == 0:
        print(f"  SKIPPED: {len(values)} samples is shorter than one {SEGMENT_SECONDS}s segment")
        return {"sample_id": sample_id, "filename": filename, "status": "skipped", "reason": "too short to segment"}

    leftover = len(values) - num_segments * SEGMENT_SAMPLES
    print(f"  Splitting into {num_segments} segment(s) of {SEGMENT_SECONDS}s, discarding {leftover} leftover samples ({leftover / SAMPLE_RATE:.2f}s)")

    uploaded = []
    for i in range(num_segments):
        start = i * SEGMENT_SAMPLES
        chunk = values[start:start + SEGMENT_SAMPLES]
        wav_bytes = values_to_wav_bytes(chunk, frequency)
        seg_filename = f"{filename}-seg{i}.wav"
        resp = upload_segment(seg_filename, wav_bytes, label)
        if resp.status_code >= 300:
            print(f"  FAILED to upload segment {i} ({seg_filename}): HTTP {resp.status_code} {resp.text[:200]}")
            return {
                "sample_id": sample_id, "filename": filename, "status": "partial_failure",
                "segments_uploaded": uploaded, "failed_at_segment": i,
            }
        print(f"  Uploaded segment {i}: {seg_filename}")
        uploaded.append(seg_filename)

    disable_resp = disable_sample(sample_id)
    if disable_resp.status_code >= 300:
        print(f"  Segments uploaded, but FAILED to disable original: HTTP {disable_resp.status_code} {disable_resp.text[:200]}")
        return {
            "sample_id": sample_id, "filename": filename, "status": "uploaded_not_disabled",
            "segments_uploaded": uploaded,
        }

    print(f"  Disabled original sample {sample_id}")
    return {
        "sample_id": sample_id, "filename": filename, "status": "done",
        "segments_uploaded": uploaded,
    }


def main():
    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)
    if not getattr(cfg, "EI_ADMIN_API_KEY", None) or cfg.EI_ADMIN_API_KEY == "your-edge-impulse-admin-api-key-here":
        print("EI_ADMIN_API_KEY is not configured in config.py (disabling samples requires an Admin-role key). Aborting.")
        sys.exit(1)

    print(f"Looking up enabled samples longer than {MIN_LENGTH_MS}ms, all labels, project-wide...")
    samples = list_long_samples()

    if not samples:
        print("No matching samples found. Nothing to do.")
        return

    print(f"\nFound {len(samples)} samples to process:")
    for s in samples:
        print(f"  - [{s['label']}] {s['filename']} ({s['totalLengthMs']}ms)")

    if "--yes" not in sys.argv:
        confirm = input(f"\nSplit and disable all {len(samples)} samples above? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted, nothing changed.")
            return

    results = [process_sample(s) for s in samples]

    print("\n--- Summary ---")
    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)
    for status, items in by_status.items():
        print(f"{status}: {len(items)}")
        for item in items:
            seg_count = len(item.get("segments_uploaded", []))
            print(f"  [{item['sample_id']}] {item['filename']} -- {seg_count} segment(s) uploaded")


if __name__ == "__main__":
    main()
