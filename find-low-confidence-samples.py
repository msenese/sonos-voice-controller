#!/usr/bin/env python3
"""Read-only diagnostic: runs the current trained model against every
enabled "unknown" and "noise" sample and flags any where the model's own
predicted confidence for that sample's ASSIGNED label falls below a
threshold. A low self-confidence score means the model, having actually
been trained on this data, still doesn't recognize the sample as a
confident example of its own label -- a strong signal of either
mislabeling or genuine acoustic ambiguity sitting right on the
noise/unknown boundary (the boundary this project keeps fighting false
triggers over).

Uses GET /classify/{sample_id} -- confirmed via direct probing to run
synchronously per sample against the currently trained model, no job
queue or polling needed (unlike /jobs/retrain etc. elsewhere in this
project). Read-only: this only classifies and reports, it never edits,
disables, or deletes anything -- flagged samples are for manual review,
same spirit as fix-3sec-samples.py logging everything up front rather
than acting silently.

Usage: python3 find-low-confidence-samples.py [--threshold 0.6] [--workers 5]
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
PAGE_SIZE = 100
TARGET_LABELS = ["unknown", "noise"]


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def list_samples(label):
    matches = []
    offset = 0
    while True:
        r = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "labels": f'["{label}"]', "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        page = r.json().get("samples", [])
        matches.extend(s for s in page if not s.get("isDisabled"))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def classify_sample(sample):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/classify/{sample['id']}",
        headers=ei_headers(),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    result = data["classifications"][0]["result"][0]
    self_confidence = result.get(sample["label"], 0.0)
    top_label, top_confidence = max(result.items(), key=lambda kv: kv[1])
    return {
        "id": sample["id"],
        "filename": sample["filename"],
        "label": sample["label"],
        "self_confidence": self_confidence,
        "top_label": top_label,
        "top_confidence": top_confidence,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)

    samples = []
    for label in TARGET_LABELS:
        found = list_samples(label)
        print(f"Found {len(found)} enabled samples labeled {label!r}")
        samples.extend(found)

    print(f"\nClassifying {len(samples)} samples against the current trained model ({args.workers} concurrent)...")

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(classify_sample, s): s for s in samples}
        done = 0
        for future in as_completed(futures):
            sample = futures[future]
            done += 1
            try:
                results.append(future.result())
            except requests.RequestException as e:
                errors.append((sample, str(e)))
            if done % 50 == 0 or done == len(samples):
                print(f"  {done}/{len(samples)} classified...")

    if errors:
        print(f"\n{len(errors)} sample(s) failed to classify (skipped, not flagged):")
        for sample, err in errors:
            print(f"  [{sample['id']}] {sample['filename']}: {err}")

    flagged = [r for r in results if r["self_confidence"] < args.threshold]
    flagged.sort(key=lambda r: r["self_confidence"])

    print(f"\n--- {len(flagged)} of {len(results)} samples below {args.threshold:.0%} self-confidence, lowest first ---\n")
    for r in flagged:
        print(
            f"  {r['self_confidence']:>6.1%}  [{r['id']}] [{r['label']}] {r['filename']}"
            f"  (model instead leans {r['top_label']!r} at {r['top_confidence']:.1%})"
        )

    if not flagged:
        print("  None -- every sample's own label was the model's confident top (or only) choice.")


if __name__ == "__main__":
    main()
