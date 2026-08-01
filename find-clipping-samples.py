#!/usr/bin/env python3
"""Read-only diagnostic: pulls every currently-enabled sample across the
whole Edge Impulse project (all labels) and flags clipping -- peak
amplitude >= CLIP_PEAK_THRESHOLD, the same absolute ceiling used by
analyze-label-samples.py. A dedicated, project-wide scan rather than
running the per-label analyzer for every label, since clipping doesn't
need the self-confidence classification pass or per-batch percentile
math those scripts do -- it's a single fixed, objective threshold, so
skipping that work makes this considerably faster for a "check
everything" pass.

Usage: python3 find-clipping-samples.py [--workers 8]
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
PAGE_SIZE = 100
CLIP_PEAK_THRESHOLD = 32000


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def list_all_samples():
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
        matches.extend(s for s in page if not s.get("isDisabled"))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def fetch_peak(sample):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample['id']}",
        headers=ei_headers(), timeout=30,
    )
    r.raise_for_status()
    values = [v[0] for v in r.json()["payload"]["values"]]
    peak = max((abs(v) for v in values), default=0)
    return {
        "id": sample["id"],
        "filename": sample["filename"],
        "label": sample.get("label", "?"),
        "category": sample.get("category"),
        "duration_ms": sample.get("totalLengthMs"),
        "added": sample.get("added"),
        "peak": peak,
    }


def find_clipping(workers):
    samples = list_all_samples()
    print(f"Found {len(samples)} enabled samples across the project. Checking peak amplitude...")

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_peak, s): s for s in samples}
        done = 0
        for future in as_completed(futures):
            sample = futures[future]
            done += 1
            try:
                results.append(future.result())
            except requests.RequestException as e:
                errors.append((sample, str(e)))
            if done % 100 == 0 or done == len(samples):
                print(f"  {done}/{len(samples)} checked...")

    if errors:
        print(f"\n{len(errors)} sample(s) failed to check (skipped):")
        for sample, err in errors:
            print(f"  [{sample['id']}] {sample['filename']}: {err}")

    clipped = sorted((r for r in results if r["peak"] >= CLIP_PEAK_THRESHOLD), key=lambda r: -r["peak"])

    print(f"\n=== Clipping (peak >= {CLIP_PEAK_THRESHOLD}): {len(clipped)} of {len(results)} samples ===\n")
    for r in clipped:
        split_marker = " [TEST]" if r["category"] == "testing" else ""
        print(
            f"  peak={r['peak']:>5}  [{r['id']}]{split_marker} {r['filename']}"
            f"  ({r['label']}, {r['duration_ms']}ms, added {r['added']})"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)

    find_clipping(args.workers)


if __name__ == "__main__":
    main()
