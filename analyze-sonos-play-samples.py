#!/usr/bin/env python3
"""Read-only diagnostic: pulls every currently-enabled "sonos play" sample
and checks each for four kinds of anomaly, in service of the observed gap
between validation (100%) and test-set (85.3%) accuracy for this label:

1. Duration -- flags unusually SHORT samples (bottom 5th percentile of
   this batch's own duration distribution), likely truncated captures.
   Not a fixed "~1.3s expected" check: this project's capture pipeline
   changed duration conventions more than once over time (a clear
   cluster of samples sits at exactly 1000ms, an older Training Mode
   default, not an anomaly), so the only duration signal that's
   actually meaningful here is relative to the rest of THIS dataset,
   not a single fixed target.
2. Amplitude -- flags clipping (peak >= 32000, an absolute ceiling --
   matches CLIP_THRESHOLD's value elsewhere in this project) and
   unusually quiet samples (below the 10th percentile of this specific
   batch's peak amplitudes, not a fixed number, since "unusually quiet"
   is relative to how this project's mic/gain actually records, not an
   arbitrary global threshold).
3. Self-confidence -- runs each sample through the current trained model
   (GET /classify/{id}, same approach as find-low-confidence-samples.py)
   and flags any where the model's own confidence in "sonos play" is
   below a threshold (default 70%).
4. Test-split membership -- the sample's own "category" field
   (training vs. testing) is reported directly so low-confidence
   testing-category samples -- the ones actually responsible for the
   validation/test gap -- are easy to pick out from the ranked list.

Ranked by self-confidence ascending (worst first) since that's the
single most informative signal here; other flags are shown inline so a
sample stacking multiple problems stands out immediately.

Usage: python3 analyze-sonos-play-samples.py [--confidence-threshold 0.7] [--workers 5]
"""
import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
PAGE_SIZE = 100
TARGET_LABEL = "sonos play"
CLIP_PEAK_THRESHOLD = 32000
QUIET_PERCENTILE = 10
SHORT_DURATION_PERCENTILE = 5


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def list_samples():
    matches = []
    offset = 0
    while True:
        r = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "labels": f'["{TARGET_LABEL}"]', "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        page = r.json().get("samples", [])
        matches.extend(s for s in page if not s.get("isDisabled"))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def fetch_peak_amplitude(sample_id):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}",
        headers=ei_headers(), timeout=30,
    )
    r.raise_for_status()
    values = [v[0] for v in r.json()["payload"]["values"]]
    return max((abs(v) for v in values), default=0)


def classify_self_confidence(sample_id):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/classify/{sample_id}",
        headers=ei_headers(), timeout=30,
    )
    r.raise_for_status()
    result = r.json()["classifications"][0]["result"][0]
    return result.get(TARGET_LABEL, 0.0)


def analyze_one(sample):
    peak = fetch_peak_amplitude(sample["id"])
    self_confidence = classify_self_confidence(sample["id"])
    return {
        "id": sample["id"],
        "filename": sample["filename"],
        "category": sample.get("category"),
        "duration_ms": sample.get("totalLengthMs"),
        "added": sample.get("added"),
        "peak": peak,
        "self_confidence": self_confidence,
    }


def percentile(values, pct):
    if not values:
        return 0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * pct / 100)))
    return s[idx]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confidence-threshold", type=float, default=0.7)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)

    samples = list_samples()
    print(f"Found {len(samples)} enabled {TARGET_LABEL!r} samples. Analyzing...")

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(analyze_one, s): s for s in samples}
        done = 0
        for future in as_completed(futures):
            sample = futures[future]
            done += 1
            try:
                results.append(future.result())
            except requests.RequestException as e:
                errors.append((sample, str(e)))
            if done % 50 == 0 or done == len(samples):
                print(f"  {done}/{len(samples)} analyzed...")

    if errors:
        print(f"\n{len(errors)} sample(s) failed to analyze (skipped):")
        for sample, err in errors:
            print(f"  [{sample['id']}] {sample['filename']}: {err}")

    peaks = [r["peak"] for r in results]
    quiet_cutoff = percentile(peaks, QUIET_PERCENTILE)
    print(f"\nPeak amplitude: min={min(peaks)}, {QUIET_PERCENTILE}th pct={quiet_cutoff}, max={max(peaks)}")

    durations = [r["duration_ms"] for r in results if r["duration_ms"] is not None]
    short_cutoff = percentile(durations, SHORT_DURATION_PERCENTILE)
    mode_duration, mode_count = Counter(durations).most_common(1)[0]
    print(
        f"Duration: min={min(durations)}ms, {SHORT_DURATION_PERCENTILE}th pct={short_cutoff:.0f}ms, "
        f"max={max(durations)}ms (most common: {mode_duration}ms x{mode_count} -- an intentional "
        f"historical convention, not itself flagged)"
    )

    for r in results:
        issues = []
        if r["duration_ms"] is not None and r["duration_ms"] <= short_cutoff:
            issues.append(f"short duration ({r['duration_ms']}ms)")
        if r["peak"] >= CLIP_PEAK_THRESHOLD:
            issues.append(f"clipping (peak {r['peak']})")
        elif r["peak"] <= quiet_cutoff:
            issues.append(f"quiet (peak {r['peak']})")
        if r["self_confidence"] < args.confidence_threshold:
            issues.append(f"low self-confidence")
        r["issues"] = issues

    results.sort(key=lambda r: r["self_confidence"])

    flagged = [r for r in results if r["issues"]]
    print(f"\n--- {len(flagged)} of {len(results)} samples flagged, lowest self-confidence first ---\n")
    for r in flagged:
        split_marker = " [TEST]" if r["category"] == "testing" else ""
        print(
            f"  {r['self_confidence']:>6.1%}  [{r['id']}]{split_marker} {r['filename']}"
            f"  -- {', '.join(r['issues'])}"
        )

    test_flagged = [r for r in flagged if r["category"] == "testing"]
    print(f"\n{len(test_flagged)} of the flagged samples are in the testing split -- these are the ones")
    print("directly responsible for the validation/test accuracy gap:")
    for r in test_flagged:
        print(f"  {r['self_confidence']:>6.1%}  [{r['id']}] {r['filename']}  -- {', '.join(r['issues'])}")


if __name__ == "__main__":
    main()
