#!/usr/bin/env python3
"""Read-only diagnostic: pulls every currently-enabled sample across the
whole Edge Impulse project (not scoped to one label -- a real-world event
captured twice under two different labels, e.g. once as "sonos play" and
once as "unknown" after a relabel, is exactly the kind of duplicate worth
catching) and flags likely duplicates two ways:

1. Exact duplicates -- sha256 of the raw PCM bytes. Catches byte-identical
   captures (e.g. the same ring-buffer snapshot uploaded twice).
2. Near-duplicates -- cosine similarity on a compact spectral fingerprint.
   The concern driving this (per the request that prompted this script) is
   Buffer mode's ring buffer capturing the same real-world utterance more
   than once via overlapping windows -- these won't be byte-identical (the
   window start/duration differs slightly) but are the same sound. A
   fingerprint built from log-magnitude energy in fixed Hz bands (not fixed
   FFT bins, since bin spacing depends on each clip's own length) is used
   instead of raw samples specifically because it's insensitive to the time
   axis: two overlapping captures of the same 1s of speech carry nearly the
   same *frequency content* even if one starts 300ms later than the other,
   where a raw-waveform or bin-index comparison would be thrown off by that
   shift. It's still just a heuristic, not proof of duplication -- always
   listen before deleting. Near-silent/ambient clips are excluded from this
   pass entirely (below the batch's own 25th-percentile RMS energy) since
   they're spectrally flat and this fingerprint can't meaningfully tell one
   quiet moment from another -- confirmed on a real run, where every one of
   the top similarity scores was a low-energy pair before this filter
   existed. Flagged as individual pairs, not merged into transitive
   clusters -- also confirmed on a real run: transitively merging any two
   samples sharing an above-threshold edge chained through one coincidental
   link and collapsed the entire 1453-sample project into a single "group".

   Acoustic similarity alone still isn't enough, though -- also confirmed on
   a real run: a short trigger phrase said twice by the same speaker in the
   same room legitimately sounds almost identical, whether or not it's the
   same overlapping-window event (519,588 pairs cleared 0.985, dominated by
   ordinary same-label matches like "sonos play" vs "sonos play"). What
   actually distinguishes "the same moment, captured twice" from "the same
   word, said again on a different day" is timing: two overlapping captures
   of one real event land within seconds of each other, not hours or days
   apart. A pair is only flagged if it clears the similarity threshold AND
   was added within --max-gap-seconds of each other.

Also prints the top raw similarity scores among the comparable (non-quiet)
samples before either cutoff, so both can be sanity-checked against this
dataset's own distribution rather than trusted blindly -- percentile/
threshold calibration has bitten this exact kind of analysis before in this
project (see analyze-label-samples.py's duration check), so this is
deliberately shown up front rather than assumed.

Usage: python3 find-duplicate-samples.py [--similarity-threshold 0.985] [--max-gap-seconds 90] [--workers 8] [--exact-only]
"""
import argparse
import hashlib
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
PAGE_SIZE = 100
DEFAULT_SIMILARITY_THRESHOLD = 0.985
DEFAULT_MAX_GAP_SECONDS = 90
QUIET_ENERGY_PERCENTILE = 25

FRAME_MS = 25
HOP_MS = 10
NUM_TIME_BINS = 12
NUM_FREQ_BANDS = 16
STANDARD_SAMPLE_RATE = 16000  # fixed for every sample in this project
FRAME_LEN = int(STANDARD_SAMPLE_RATE * FRAME_MS / 1000)
HOP = int(STANDARD_SAMPLE_RATE * HOP_MS / 1000)
# Log-spaced Hz band edges, not raw FFT bin indices -- bin spacing depends
# on frame length, but these Hz ranges mean the same thing regardless.
# Upper bound is 8000Hz (Nyquist for this project's fixed 16kHz rate).
FREQ_BAND_EDGES = np.geomspace(50, 8000, num=NUM_FREQ_BANDS + 1)
MAX_PRINTED_PAIRS = 300

_frame_window = np.hanning(FRAME_LEN)
_freqs = np.fft.rfftfreq(FRAME_LEN, d=1.0 / STANDARD_SAMPLE_RATE)
_band_idx = np.clip(np.digitize(_freqs, FREQ_BAND_EDGES) - 1, 0, NUM_FREQ_BANDS - 1)
# One-hot averaging matrix: a frame's magnitude spectrum @ this collapses
# straight to per-band means in a single matmul, instead of a Python-level
# loop per band per frame -- matters here since it runs once per frame per
# sample, across roughly 1500 samples.
_band_matrix = np.zeros((len(_freqs), NUM_FREQ_BANDS))
_band_matrix[np.arange(len(_freqs)), _band_idx] = 1.0
_band_matrix /= np.maximum(_band_matrix.sum(axis=0), 1)


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


def extract_fingerprint(values, sample_rate):
    # A whole-clip (no time axis) version of this was tried first and
    # failed badly on a real run: averaging frequency content across an
    # entire clip mostly just captures "a human voice, through this mic, in
    # this room" rather than anything specific to the moment, so almost
    # every non-quiet sample in the project ended up looking similar to
    # almost every other one (~780,000 flagged pairs). This keeps a coarse
    # time axis -- a small time x frequency grid -- so different phrases
    # and different moments actually look different, while still being
    # coarse enough (12 time bins across the whole clip) to tolerate the
    # few-hundred-ms shift expected between two overlapping captures of the
    # same real-world moment.
    if sample_rate != STANDARD_SAMPLE_RATE:
        return None  # everything in this project is captured at 16kHz
    samples = np.array(values, dtype=np.float64)
    if len(samples) < FRAME_LEN:
        return None
    samples -= samples.mean()

    num_frames = 1 + (len(samples) - FRAME_LEN) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME_LEN)[::HOP][:num_frames]
    spectrum = np.abs(np.fft.rfft(frames * _frame_window, axis=1))
    frame_bands = spectrum @ _band_matrix  # (num_frames, NUM_FREQ_BANDS)

    # Resampled to a fixed number of time bins by proportional position, not
    # raw frame count -- keeps the fingerprint duration-invariant (a 1.3s
    # and a 4s clip both come out the same shape).
    time_bin_edges = np.linspace(0, num_frames, NUM_TIME_BINS + 1).astype(int)
    grid = np.zeros((NUM_TIME_BINS, NUM_FREQ_BANDS))
    for t in range(NUM_TIME_BINS):
        lo, hi = time_bin_edges[t], max(time_bin_edges[t] + 1, time_bin_edges[t + 1])
        grid[t] = frame_bands[lo:hi].mean(axis=0)

    fingerprint = np.log1p(grid).flatten()
    norm = np.linalg.norm(fingerprint)
    return fingerprint / norm if norm > 0 else None


def parse_added(added):
    if not added:
        return None
    try:
        return datetime.strptime(added.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def analyze_one(sample):
    r = requests.get(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample['id']}",
        headers=ei_headers(), timeout=30,
    )
    r.raise_for_status()
    payload = r.json()["payload"]
    interval_ms = payload.get("interval_ms") or (1000.0 / 16000)
    sample_rate = round(1000.0 / interval_ms) if interval_ms else 16000
    values = [v[0] for v in payload["values"]]

    content_hash = hashlib.sha256(np.array(values, dtype=np.int16).tobytes()).hexdigest()
    fingerprint = extract_fingerprint(values, sample_rate)
    rms_energy = float(np.sqrt(np.mean(np.square(np.array(values, dtype=np.float64)))))

    return {
        "id": sample["id"],
        "filename": sample["filename"],
        "label": sample.get("label", "?"),
        "category": sample.get("category"),
        "duration_ms": sample.get("totalLengthMs"),
        "added": sample.get("added"),
        "added_dt": parse_added(sample.get("added")),
        "content_hash": content_hash,
        "fingerprint": fingerprint,
        "rms_energy": rms_energy,
    }


def percentile(values, pct):
    if not values:
        return 0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * pct / 100)))
    return s[idx]


def describe(r):
    split_marker = " [TEST]" if r["category"] == "testing" else ""
    return f"[{r['id']}]{split_marker} {r['filename']}  ({r['label']}, {r['duration_ms']}ms, added {r['added']})"


def find_duplicates(similarity_threshold, max_gap_seconds, workers, exact_only):
    samples = list_all_samples()
    print(f"Found {len(samples)} enabled samples across the project. Fetching + fingerprinting...")

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_one, s): s for s in samples}
        done = 0
        for future in as_completed(futures):
            sample = futures[future]
            done += 1
            try:
                r = future.result()
                if r["fingerprint"] is not None:
                    results.append(r)
                else:
                    errors.append((sample, "too short to fingerprint"))
            except requests.RequestException as e:
                errors.append((sample, str(e)))
            if done % 50 == 0 or done == len(samples):
                print(f"  {done}/{len(samples)} analyzed...")

    if errors:
        print(f"\n{len(errors)} sample(s) skipped:")
        for sample, err in errors:
            print(f"  [{sample['id']}] {sample['filename']}: {err}")

    if not results:
        print("No samples to compare.")
        return

    # --- Exact duplicates (hash match) ---
    by_hash = {}
    for r in results:
        by_hash.setdefault(r["content_hash"], []).append(r)
    exact_groups = [g for g in by_hash.values() if len(g) > 1]
    exact_ids = {r["id"] for g in exact_groups for r in g}

    print(f"\n=== Exact duplicates (identical audio bytes): {len(exact_groups)} group(s) ===")
    for g in sorted(exact_groups, key=lambda g: -len(g)):
        print(f"\n  Group of {len(g)}:")
        for r in g:
            print(f"    {describe(r)}")

    if exact_only:
        return

    # --- Near-duplicates (cosine similarity) ---
    #
    # Near-silent/ambient clips are spectrally flat and largely featureless,
    # so this fingerprint can't distinguish "the same quiet moment captured
    # twice" from "two different quiet moments that both sound like room
    # tone" -- comparing them just floods the results with meaningless
    # near-1.0 scores (confirmed empirically: every one of the top scores on
    # an unfiltered run was a low-energy pair). Excluding the quietest slice
    # of this batch's own RMS-energy distribution keeps the comparison to
    # samples where spectral shape actually carries signal.
    energies = [r["rms_energy"] for r in results]
    quiet_floor = percentile(energies, QUIET_ENERGY_PERCENTILE)
    comparable = [r for r in results if r["rms_energy"] > quiet_floor]
    print(f"\nRMS energy: {QUIET_ENERGY_PERCENTILE}th pct={quiet_floor:.0f} -- "
          f"excluding {len(results) - len(comparable)} near-silent sample(s) below it from the "
          f"similarity comparison (still covered by the exact-hash pass above).")

    matrix = np.stack([r["fingerprint"] for r in comparable])
    sim = matrix @ matrix.T
    n = len(comparable)

    pairs = [(sim[i, j], i, j) for i in range(n) for j in range(i + 1, n)]
    pairs.sort(key=lambda p: -p[0])

    print(f"\n=== Top similarity scores among comparable samples (for calibrating the threshold) ===")
    for score, i, j in pairs[:30]:
        print(f"  {score:.4f}  {comparable[i]['id']} <-> {comparable[j]['id']}"
              f"  ({comparable[i]['label']} / {comparable[j]['label']})")

    # Reported as individual pairs rather than transitively merged into
    # clusters -- merging any two samples that share an above-threshold edge
    # (A~B, B~C implies A,B,C are "one group") chains through the whole
    # dataset in practice, since it only takes one coincidental link anywhere
    # to merge two otherwise-unrelated clusters. A confirmed real failure on
    # an earlier run of this exact script: it merged all 1453 samples in the
    # project into a single "group". Pairs are what's actually being asked
    # for here (two overlapping captures of one event), so that's what's
    # reported -- a genuine burst of 3+ near-identical captures will simply
    # show up as multiple overlapping pairs.
    #
    # Similarity alone isn't enough either -- also confirmed on a real run:
    # a short trigger phrase said twice by the same speaker legitimately
    # sounds almost identical whether or not it's the same overlapping-
    # window event, which flooded this with ordinary same-label matches
    # (519,588 pairs at this same threshold before this filter existed).
    # Requiring the pair to also have been added within max_gap_seconds of
    # each other is what actually targets "the same moment, captured
    # twice" rather than "the same word, said again on a different day".
    # `pairs` is sorted descending by score, so this can stop at the first
    # pair below threshold instead of scanning the full list.
    flagged = []
    for score, i, j in pairs:
        if score < similarity_threshold:
            break
        a, b = comparable[i], comparable[j]
        if a["added_dt"] is None or b["added_dt"] is None:
            continue
        gap = abs((a["added_dt"] - b["added_dt"]).total_seconds())
        if gap <= max_gap_seconds:
            flagged.append((score, i, j))

    print(f"\n=== Near-duplicate pairs (cosine similarity >= {similarity_threshold}, "
          f"added within {max_gap_seconds}s of each other): {len(flagged)} pair(s) ===")
    if len(flagged) > MAX_PRINTED_PAIRS:
        # A hard cap independent of how well-tuned the threshold/fingerprint
        # turn out to be -- a mis-tuned run should degrade to "too many
        # results to be useful, raise the threshold," never to a runaway
        # multi-hundred-MB output file. That happened for real on an earlier
        # version of this script's fingerprint (~780,000 flagged pairs, a
        # 128MB output file) before this cap existed.
        print(f"  (showing the top {MAX_PRINTED_PAIRS} by similarity -- "
              f"that many flagged pairs likely means the threshold needs raising)")
    for score, i, j in flagged[:MAX_PRINTED_PAIRS]:
        a, b = comparable[i], comparable[j]
        gap = abs((a["added_dt"] - b["added_dt"]).total_seconds())
        tag = " [overlaps an exact-duplicate group]" if a["id"] in exact_ids or b["id"] in exact_ids else ""
        print(f"\n  Similarity {score:.4f}, added {gap:.0f}s apart{tag}:")
        print(f"    {describe(a)}")
        print(f"    {describe(b)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--similarity-threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD)
    parser.add_argument("--max-gap-seconds", type=float, default=DEFAULT_MAX_GAP_SECONDS,
                         help="only flag a pair if also added within this many seconds of each other")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--exact-only", action="store_true", help="skip the near-duplicate pass, hash-match only")
    args = parser.parse_args()

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)

    find_duplicates(args.similarity_threshold, args.max_gap_seconds, args.workers, args.exact_only)


if __name__ == "__main__":
    main()
