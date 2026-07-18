#!/usr/bin/env python3
"""One-off cleanup: delete every sample across all four labels (noise,
sonos pause, sonos play, unknown) added to the project before a cutoff
date, on the basis that everything recorded on the RP2040 device was
uploaded before that date.

Filters on Edge Impulse's own "added" timestamp for each sample (an ISO
8601 string, e.g. "2026-07-18T15:14:44.895Z") rather than any filename
pattern -- a filename-based heuristic risked also matching samples
recorded directly in Edge Impulse Studio with the ReSpeaker HAT, which
share the same default Studio naming shape regardless of which device
recorded them. Date is the reliable signal here, not filename.

The RP2040 device recorded at a much lower level than the ReSpeaker HAT
this project actually deploys on (near-silent waveforms vs. full-amplitude
ones for the same words), which taught the model that the shared "sonos"
prefix alone -- with a too-quiet-to-matter tail -- was still a valid
"sonos play"/"sonos pause" example. That caused real false triggers on
just the word "sonos" alone. This removes that data everywhere rather
than leaving it to cause the same kind of surprise in another class later.

Usage: python3 cleanup-rp2040-samples.py <cutoff-date YYYY-MM-DD> [--yes]
"""
import sys
from datetime import datetime, timezone

import requests

import config as cfg

EI_API_BASE = "https://studio.edgeimpulse.com/v1/api"
LABELS = ["noise", "sonos pause", "sonos play", "unknown"]
PAGE_SIZE = 100


def ei_headers():
    return {"x-api-key": cfg.EI_API_KEY}


def ei_admin_headers():
    return {"x-api-key": cfg.EI_ADMIN_API_KEY}


def parse_added(added_str):
    # Edge Impulse returns e.g. "2026-07-18T15:14:44.895Z"
    return datetime.fromisoformat(added_str.replace("Z", "+00:00"))


def list_samples_before(label, cutoff):
    matches = []
    offset = 0
    while True:
        response = requests.get(
            f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data",
            headers=ei_headers(),
            params={"category": "all", "labels": f'["{label}"]', "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(data.get("error", "Edge Impulse API call failed"))
        page = data.get("samples", [])
        for s in page:
            added = s.get("added")
            if added and parse_added(added) < cutoff:
                matches.append(s)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return matches


def delete_sample(sample_id):
    return requests.delete(
        f"{EI_API_BASE}/{cfg.EI_PROJECT_ID}/raw-data/{sample_id}",
        headers=ei_admin_headers(),
        timeout=15,
    )


def main():
    args = [a for a in sys.argv[1:] if a != "--yes"]
    if not args:
        print("Usage: python3 cleanup-rp2040-samples.py <cutoff-date YYYY-MM-DD> [--yes]")
        sys.exit(1)
    cutoff = datetime.strptime(args[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if not getattr(cfg, "EI_API_KEY", None) or cfg.EI_API_KEY == "your-edge-impulse-api-key-here":
        print("EI_API_KEY is not configured in config.py. Aborting.")
        sys.exit(1)
    if not getattr(cfg, "EI_ADMIN_API_KEY", None) or cfg.EI_ADMIN_API_KEY == "your-edge-impulse-admin-api-key-here":
        print("EI_ADMIN_API_KEY is not configured in config.py (deleting samples requires an Admin-role key). Aborting.")
        sys.exit(1)

    print(f"Looking up samples added before {cutoff.date()} across all labels...")
    all_matches = []
    for label in LABELS:
        matches = list_samples_before(label, cutoff)
        if matches:
            print(f"  {label}: {len(matches)} samples")
        all_matches.extend(matches)

    if not all_matches:
        print("No matching samples found. Nothing to delete.")
        return

    print(f"\nFound {len(all_matches)} samples added before {cutoff.date()} to delete:")
    for s in all_matches:
        print(f"  - {s['filename']} (added {s['added']}, id {s['id']})")

    if "--yes" not in sys.argv:
        confirm = input(f"\nDelete all {len(all_matches)} samples above? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted, nothing deleted.")
            return

    deleted = 0
    failed = 0
    for s in all_matches:
        response = delete_sample(s["id"])
        if response.status_code >= 300:
            print(f"Failed to delete {s['filename']} (id {s['id']}): {response.status_code} {response.text[:200]}")
            failed += 1
        else:
            deleted += 1

    print(f"Done. {deleted} deleted, {failed} failed.")


if __name__ == "__main__":
    main()
